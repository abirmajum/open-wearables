"""Regression tests for failed requests and unreceived duplicate backfills."""

from collections.abc import Generator
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest

from app.integrations.celery.tasks.garmin.backfill_task import trigger_next_pending_type
from app.integrations.celery.tasks.garmin.backfill_timeout import check_triggered_timeout
from app.integrations.celery.tasks.garmin.backfill_trigger import trigger_backfill_for_type
from app.integrations.redis_client import get_redis_client
from app.services.providers.garmin.backfill_config import BACKFILL_DATA_TYPES, MAX_BACKFILL_FAILURES, REDIS_TTL
from app.services.providers.garmin.backfill_state import (
    _get_key,
    enter_retry_phase,
    get_pending_types,
    init_window_state,
    mark_type_failed,
    mark_type_success,
    persist_window_results,
    setup_retry_window,
)

TRIGGER = "app.integrations.celery.tasks.garmin.backfill_trigger"
CHAIN = "app.integrations.celery.tasks.garmin.backfill_task"
TIMEOUT = "app.integrations.celery.tasks.garmin.backfill_timeout"


@pytest.fixture
def backfill_user() -> str:
    user_id = str(uuid4())
    init_window_state(user_id)
    for data_type in BACKFILL_DATA_TYPES:
        if data_type != "sleeps":
            mark_type_success(user_id, data_type)
    return user_id


@pytest.fixture
def trigger_service() -> Generator[MagicMock, None, None]:
    with (
        patch(f"{TRIGGER}.SessionLocal"),
        patch(f"{TRIGGER}.UserConnectionRepository"),
        patch(f"{TRIGGER}.GarminOAuth"),
        patch(f"{TRIGGER}.GarminBackfillService") as service,
        patch(f"{TRIGGER}._record_accepted_window"),
        patch(f"{TRIGGER}.celery_app"),
        patch(f"{TRIGGER}.check_triggered_timeout.apply_async"),
    ):
        yield service.return_value.trigger_backfill


@pytest.mark.parametrize("failure", [429, 500, None, "exception"])
def test_failed_requests_are_retried_until_bounded(
    backfill_user: str, trigger_service: MagicMock, failure: int | str | None
) -> None:
    if failure == "exception":
        trigger_service.side_effect = RuntimeError("request interrupted")
    else:
        trigger_service.return_value = {
            "failed": {"sleeps": "upstream failure"},
            "failed_status_codes": {"sleeps": failure},
        }

    for attempt in range(MAX_BACKFILL_FAILURES):
        trigger_backfill_for_type(backfill_user, "sleeps")
        persist_window_results(backfill_user, 0)
        assert get_redis_client().get(_get_key(backfill_user, "w", "0", "sleeps", "status")) == "failed"
        if attempt < MAX_BACKFILL_FAILURES - 1:
            assert get_pending_types(backfill_user) == ["sleeps"]
            with patch(f"{CHAIN}.trigger_backfill_for_type.apply_async") as dispatch:
                trigger_next_pending_type(backfill_user)
            assert dispatch.call_args.kwargs["args"] == [backfill_user, "sleeps"]
        else:
            assert get_pending_types(backfill_user) == []


def test_failed_request_can_recover(backfill_user: str, trigger_service: MagicMock) -> None:
    mark_type_failed(backfill_user, "sleeps", "temporary failure")
    trigger_service.return_value = {"triggered": ["sleeps"], "failed": {}}
    assert trigger_backfill_for_type(backfill_user, "sleeps")["status"] == "triggered"
    mark_type_success(backfill_user, "sleeps")
    persist_window_results(backfill_user, 0)
    assert get_pending_types(backfill_user) == []
    assert get_redis_client().get(_get_key(backfill_user, "w", "0", "sleeps", "status")) == "done"


@pytest.mark.parametrize("data_received", [False, True])
def test_timeout_retry_duplicate_requires_data(
    backfill_user: str, trigger_service: MagicMock, data_received: bool
) -> None:
    enter_retry_phase(backfill_user, [])
    setup_retry_window(backfill_user, 0)
    trigger_service.return_value = {"duplicate": ["sleeps"], "triggered": [], "failed": {}}
    assert trigger_backfill_for_type(backfill_user, "sleeps")["status"] == "triggered"
    assert get_redis_client().get(_get_key(backfill_user, "types", "sleeps", "status")) == "triggered"
    if data_received:
        mark_type_success(backfill_user, "sleeps")
    expired = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
    get_redis_client().setex(_get_key(backfill_user, "types", "sleeps", "triggered_at"), REDIS_TTL, expired)
    with patch(f"{TIMEOUT}.celery_app"):
        result = check_triggered_timeout(backfill_user, "sleeps")
    assert result["status"] == ("already_resolved" if data_received else "timed_out")
    assert get_pending_types(backfill_user) == ([] if data_received else ["sleeps"])


def test_failure_budget_is_per_window_and_resets_for_new_backfill(backfill_user: str) -> None:
    for _ in range(MAX_BACKFILL_FAILURES):
        mark_type_failed(backfill_user, "sleeps", "unavailable")
    assert get_pending_types(backfill_user) == []
    setup_retry_window(backfill_user, 1)
    assert get_pending_types(backfill_user) == ["sleeps"]
    get_redis_client().delete(_get_key(backfill_user, "retry_current_window"))
    init_window_state(backfill_user)
    assert get_pending_types(backfill_user) == ["sleeps"]
