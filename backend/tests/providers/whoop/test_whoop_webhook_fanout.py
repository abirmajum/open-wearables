"""WHOOP webhook fan-out to every OW profile linked to one WHOOP account.

Regression: process_payload resolved a single connection (the oldest), so a WHOOP
account linked to a stale profile and a live profile stored the record and emitted
outgoing events only for the stale one. The live profile waited for the hourly pull.
All payloads are synthetic; no provider credentials are used.
"""

from collections.abc import Iterator
from datetime import datetime, timedelta, timezone
from typing import Any
from unittest.mock import MagicMock, patch
from uuid import UUID, uuid4

import pytest
from celery.exceptions import Retry
from sqlalchemy.orm import Session

import app.integrations.celery.tasks.webhook_push_task as task
from app.models import DataSource, EventRecord, HealthScore, User, UserConnection
from app.repositories import EventRecordRepository, UserConnectionRepository
from app.schemas.enums import HealthScoreCategory, ProviderName
from app.schemas.webhooks.event_types import WebhookEventType
from app.services.health_score_service import health_score_service
from app.services.provider_cycle_service import provider_cycle_service
from app.services.providers.whoop.data_247 import Whoop247Data
from app.services.providers.whoop.webhook_handler import WhoopWebhookHandler
from app.services.providers.whoop.workouts import WhoopWorkouts
from app.utils.exceptions import ProviderError, ProviderNotConnectedError
from tests.factories import DataSourceFactory, EventRecordFactory, UserConnectionFactory, UserFactory

WHOOP_USER_ID = 15469532
TRACE_ID = "trace-fanout"


def workout_fixture(workout_id: str) -> dict[str, Any]:
    return {
        "id": workout_id,
        "user_id": WHOOP_USER_ID,
        "created_at": "2026-10-12T08:00:00Z",
        "updated_at": "2026-10-12T08:05:00Z",
        "start": "2026-10-12T07:00:00Z",
        "end": "2026-10-12T07:45:00Z",
        "timezone_offset": "+03:00",
        "sport_name": "running",
        "sport_id": 0,
        "score_state": "SCORED",
        "score": {"strain": 11.4, "average_heart_rate": 150, "max_heart_rate": 178, "kilojoule": 2100.0},
    }


def sleep_fixture(sleep_id: str, cycle_id: int) -> dict[str, Any]:
    return {
        "id": sleep_id,
        "cycle_id": cycle_id,
        "start": "2026-10-11T22:00:00Z",
        "end": "2026-10-12T05:30:00Z",
        "timezone_offset": "+03:00",
        "nap": False,
        "score_state": "SCORED",
        "score": {
            "sleep_performance_percentage": 84,
            "sleep_efficiency_percentage": 92,
            "stage_summary": {
                "total_light_sleep_time_milli": 12600000,
                "total_slow_wave_sleep_time_milli": 6300000,
                "total_rem_sleep_time_milli": 5400000,
                "total_awake_time_milli": 1800000,
            },
        },
    }


def open_cycle_fixture(cycle_id: int) -> dict[str, Any]:
    return {
        "id": cycle_id,
        "user_id": WHOOP_USER_ID,
        "start": "2026-10-11T22:00:00Z",
        "updated_at": "2026-10-12T06:00:00Z",
        "end": None,
        "timezone_offset": "+03:00",
        "score_state": "SCORED",
        "score": {"strain": 4.2, "kilojoule": 3100.0, "average_heart_rate": 64, "max_heart_rate": 120},
    }


@pytest.fixture
def handler() -> WhoopWebhookHandler:
    oauth = MagicMock()
    data_247 = Whoop247Data("whoop", "https://fictional.example", oauth)
    workouts = WhoopWorkouts(
        workout_repo=EventRecordRepository(EventRecord),
        connection_repo=UserConnectionRepository(),
        provider_name="whoop",
        api_base_url="https://fictional.example",
        oauth=oauth,
    )
    return WhoopWebhookHandler(data_247, workouts)


@pytest.fixture
def connections(db: Session) -> list[UserConnection]:
    """A stale legacy profile (older) and the live profile, both linked to one WHOOP account."""
    now = datetime.now(timezone.utc)
    stale = UserConnectionFactory(
        user=UserFactory(), provider="whoop", provider_user_id=str(WHOOP_USER_ID), created_at=now - timedelta(days=30)
    )
    live = UserConnectionFactory(user=UserFactory(), provider="whoop", provider_user_id=str(WHOOP_USER_ID))
    # Committed like real rows, so a per-profile rollback cannot discard them.
    db.commit()
    return [stale, live]


@pytest.fixture
def svix_enabled() -> Iterator[None]:
    with patch("app.services.outgoing_webhooks.svix.is_enabled", return_value=True):
        yield


def _records_for(db: Session, user_id: UUID, external_id: str) -> list[EventRecord]:
    return (
        db.query(EventRecord)
        .join(DataSource, EventRecord.data_source_id == DataSource.id)
        .filter(DataSource.user_id == user_id, EventRecord.external_id == external_id)
        .all()
    )


def _emitted(mock_webhook_dispatch: MagicMock, event_type: str) -> set[str]:
    """User IDs that received an outgoing event of the given type."""
    return {
        call.args[1]["data"]["user_id"]
        for call in mock_webhook_dispatch.delay.call_args_list
        if call.args[0] == event_type
    }


@pytest.mark.usefixtures("svix_enabled")
def test_workout_saved_and_emitted_for_every_linked_profile(
    db: Session,
    handler: WhoopWebhookHandler,
    connections: list[UserConnection],
    mock_webhook_dispatch: MagicMock,
) -> None:
    workout_id = str(uuid4())
    payload = {"user_id": WHOOP_USER_ID, "id": workout_id, "type": "workout.updated"}

    with patch.object(
        handler.workouts, "get_workout_detail_from_api", return_value=workout_fixture(workout_id)
    ) as fetch:
        result = handler.process_payload(db, payload, TRACE_ID)

    # Fetched once, saved for both.
    assert fetch.call_count == 1
    assert [p["status"] for p in result["profiles"]] == ["processed", "processed"]
    assert [p["user_id"] for p in result["profiles"]] == [str(c.user_id) for c in connections]
    assert all(p["records_saved"] == 1 for p in result["profiles"])
    for connection in connections:
        assert len(_records_for(db, connection.user_id, workout_id)) == 1
        db.refresh(connection)
        assert connection.last_synced_at is not None
    assert _emitted(mock_webhook_dispatch, WebhookEventType.WORKOUT_CREATED) == {str(c.user_id) for c in connections}


def test_one_profile_failing_does_not_block_the_others(
    db: Session,
    handler: WhoopWebhookHandler,
    connections: list[UserConnection],
) -> None:
    stale, live = connections
    workout_id = str(uuid4())
    payload = {"user_id": WHOOP_USER_ID, "id": workout_id, "type": "workout.updated"}
    save = handler.workouts.save_single_workout

    def fail_for_stale(db_: Session, user_id: UUID, raw: dict[str, Any]) -> int:
        if user_id == stale.user_id:
            raise RuntimeError("synthetic write failure")
        return save(db_, user_id, raw)

    with (
        patch.object(handler.workouts, "get_workout_detail_from_api", return_value=workout_fixture(workout_id)),
        patch.object(handler.workouts, "save_single_workout", side_effect=fail_for_stale),
        pytest.raises(RuntimeError) as failure,
    ):
        handler.process_payload(db, payload, TRACE_ID)

    by_user = {p["user_id"]: p for p in getattr(failure.value, "profiles")}
    assert by_user[str(stale.user_id)]["status"] == "error"
    assert by_user[str(stale.user_id)]["error"] == "RuntimeError"
    assert by_user[str(live.user_id)]["status"] == "processed"
    assert _records_for(db, stale.user_id, workout_id) == []
    assert len(_records_for(db, live.user_id, workout_id)) == 1


def test_every_profile_failing_raises_for_retry(
    db: Session,
    handler: WhoopWebhookHandler,
    connections: list[UserConnection],
) -> None:
    workout_id = str(uuid4())
    payload = {"user_id": WHOOP_USER_ID, "id": workout_id, "type": "workout.updated"}

    with (
        patch.object(handler.workouts, "get_workout_detail_from_api", return_value=workout_fixture(workout_id)),
        patch.object(handler.workouts, "save_single_workout", side_effect=RuntimeError("synthetic write failure")),
        pytest.raises(RuntimeError),
    ):
        handler.process_payload(db, payload, TRACE_ID)


def test_fetch_falls_back_to_next_profile_token(
    db: Session,
    handler: WhoopWebhookHandler,
    connections: list[UserConnection],
) -> None:
    """A stale profile's broken token must not stop the live profile from getting data."""
    stale, live = connections
    stale.last_synced_at = datetime.now(timezone.utc)
    db.commit()
    workout_id = str(uuid4())
    payload = {"user_id": WHOOP_USER_ID, "id": workout_id, "type": "workout.updated"}

    def fetch(db_: Session, user_id: UUID, resource_id: str) -> dict[str, Any]:
        if user_id == stale.user_id:
            raise ProviderNotConnectedError("whoop")
        return workout_fixture(resource_id)

    with patch.object(handler.workouts, "get_workout_detail_from_api", side_effect=fetch) as fetch_mock:
        result = handler.process_payload(db, payload, TRACE_ID)

    assert fetch_mock.call_count == 2
    assert [p["status"] for p in result["profiles"]] == ["processed", "processed"]
    for connection in connections:
        assert len(_records_for(db, connection.user_id, workout_id)) == 1


def test_missing_resource_is_not_refetched_per_profile(
    db: Session,
    handler: WhoopWebhookHandler,
    connections: list[UserConnection],
) -> None:
    payload = {"user_id": WHOOP_USER_ID, "id": str(uuid4()), "type": "workout.updated"}

    with (
        patch.object(
            handler.workouts, "get_workout_detail_from_api", side_effect=ProviderError("whoop", upstream_status=404)
        ) as fetch,
        pytest.raises(ProviderError),
    ):
        handler.process_payload(db, payload, TRACE_ID)
    assert fetch.call_count == 1


def test_deleted_event_removes_record_for_every_linked_profile(
    db: Session,
    handler: WhoopWebhookHandler,
    connections: list[UserConnection],
) -> None:
    workout_id = str(uuid4())
    for connection in connections:
        source = DataSourceFactory(user=db.get(User, connection.user_id), provider=ProviderName.WHOOP, source="whoop")
        EventRecordFactory(data_source=source, external_id=workout_id)
    payload = {"user_id": WHOOP_USER_ID, "id": workout_id, "type": "workout.deleted"}

    result = handler.process_payload(db, payload, TRACE_ID)

    assert [p["status"] for p in result["profiles"]] == ["deleted", "deleted"]
    for connection in connections:
        assert _records_for(db, connection.user_id, workout_id) == []


def test_deleted_event_reports_each_profile_separately(
    db: Session,
    handler: WhoopWebhookHandler,
    connections: list[UserConnection],
) -> None:
    """Only the live profile has the record; the stale profile reports not-found, not an error."""
    stale, live = connections
    workout_id = str(uuid4())
    source = DataSourceFactory(user=db.get(User, live.user_id), provider=ProviderName.WHOOP, source="whoop")
    EventRecordFactory(data_source=source, external_id=workout_id)
    payload = {"user_id": WHOOP_USER_ID, "id": workout_id, "type": "workout.deleted"}

    result = handler.process_payload(db, payload, TRACE_ID)

    by_user = {p["user_id"]: p for p in result["profiles"]}
    assert by_user[str(stale.user_id)] == {
        "status": "ignored",
        "reason": "record_not_found",
        "user_id": str(stale.user_id),
    }
    assert by_user[str(live.user_id)]["status"] == "deleted"
    assert _records_for(db, live.user_id, workout_id) == []


@pytest.mark.usefixtures("svix_enabled")
def test_sleep_and_open_cycle_saved_for_every_linked_profile(
    db: Session,
    handler: WhoopWebhookHandler,
    connections: list[UserConnection],
    mock_webhook_dispatch: MagicMock,
) -> None:
    sleep_id = str(uuid4())
    payload = {"user_id": WHOOP_USER_ID, "id": sleep_id, "type": "sleep.updated"}

    with (
        patch.object(handler.data_247, "get_sleep_record", return_value=sleep_fixture(sleep_id, 501)) as get_sleep,
        patch.object(handler.data_247, "get_recent_cycles", return_value=[]) as get_recent,
        patch.object(handler.data_247, "get_cycle_record", return_value=open_cycle_fixture(501)) as get_cycle,
    ):
        result = handler.process_payload(db, payload, TRACE_ID)

    assert get_sleep.call_count == 1
    assert get_cycle.call_count == 1
    assert get_recent.call_count == 1
    assert [p["status"] for p in result["profiles"]] == ["processed", "processed"]
    for connection in connections:
        assert len(_records_for(db, connection.user_id, sleep_id)) == 1
        [cycle] = provider_cycle_service.list_whoop(db, connection.user_id)
        assert cycle.strain == 4.2
        assert cycle.provisional
        assert (
            db.query(HealthScore)
            .filter_by(user_id=connection.user_id, provider=ProviderName.WHOOP, category=HealthScoreCategory.STRAIN)
            .count()
            == 0
        )
    assert _emitted(mock_webhook_dispatch, WebhookEventType.SLEEP_CREATED) == {str(c.user_id) for c in connections}


def test_unlinked_whoop_user_is_reported(db: Session, handler: WhoopWebhookHandler) -> None:
    payload = {"user_id": 999, "id": str(uuid4()), "type": "workout.updated"}
    result = handler.process_payload(db, payload, TRACE_ID)
    assert result["status"] == "user_not_found"


@pytest.mark.usefixtures("svix_enabled")
def test_partial_write_retry_targets_only_failed_profile_without_duplicate_events(
    db: Session, handler: WhoopWebhookHandler, connections: list[UserConnection], mock_webhook_dispatch: MagicMock
) -> None:
    stale, live = connections
    stale_id, live_id = str(stale.user_id), str(live.user_id)
    workout_id = str(uuid4())
    payload = {"user_id": WHOOP_USER_ID, "id": workout_id, "type": "workout.updated"}
    save_score = health_score_service
    original = save_score.upsert_event_score

    def fail_after_workout(db_: Session, score: Any) -> Any:
        if str(score.user_id) == stale_id:
            raise RuntimeError("synthetic write failure")
        return original(db_, score)

    with (
        patch.object(task, "SessionLocal") as session_local,
        patch.object(task, "ProviderFactory") as factory,
        patch.object(task, "_emit_webhook_sync_status") as sync_log,
        patch.object(task.process_webhook_push, "retry", side_effect=Retry()) as retry,
        patch.object(handler.workouts, "get_workout_detail_from_api", return_value=workout_fixture(workout_id)),
    ):
        session_local.return_value.__enter__.return_value = db
        factory.return_value.get_provider.return_value.webhooks = handler
        with patch.object(save_score, "upsert_event_score", side_effect=fail_after_workout), pytest.raises(Retry):
            task.process_webhook_push.run("whoop", payload, TRACE_ID)
        assert _records_for(db, stale.user_id, workout_id) == []
        assert len(_records_for(db, live.user_id, workout_id)) == 1
        assert _emitted(mock_webhook_dispatch, WebhookEventType.WORKOUT_CREATED) == {live_id}
        assert [c.args[1]["user_id"] for c in sync_log.call_args_list] == [live_id]
        kwargs = retry.call_args.kwargs["kwargs"]
        assert kwargs["retry_user_ids"] == [stale_id]
        result = task.process_webhook_push.run(**kwargs)
        assert [p["user_id"] for p in result["profiles"]] == [stale_id]
        assert [c.args[1]["user_id"] for c in sync_log.call_args_list] == [live_id, stale_id]
        created = [
            call
            for call in mock_webhook_dispatch.delay.call_args_list
            if call.args[0] == WebhookEventType.WORKOUT_CREATED
        ]
        assert len(created) == 2
        assert _emitted(mock_webhook_dispatch, WebhookEventType.WORKOUT_CREATED) == {stale_id, live_id}
        task.process_webhook_push.run("whoop", payload, TRACE_ID)
        assert (
            len(
                [
                    call
                    for call in mock_webhook_dispatch.delay.call_args_list
                    if call.args[0] == WebhookEventType.WORKOUT_CREATED
                ]
            )
            == 2
        )


@pytest.mark.parametrize("status", [404, 429, 503])
def test_shared_fetch_error_is_not_repeated_for_each_token(
    db: Session, handler: WhoopWebhookHandler, connections: list[UserConnection], status: int
) -> None:
    payload = {"user_id": WHOOP_USER_ID, "id": str(uuid4()), "type": "workout.updated"}
    with (
        patch.object(
            handler.workouts, "get_workout_detail_from_api", side_effect=ProviderError("whoop", upstream_status=status)
        ) as fetch,
        pytest.raises(ProviderError),
    ):
        handler.process_payload(db, payload, TRACE_ID)
    fetch.assert_called_once()


def test_exhausted_profile_retries_report_each_failure(
    db: Session, handler: WhoopWebhookHandler, connections: list[UserConnection]
) -> None:
    payload = {"user_id": WHOOP_USER_ID, "id": str(uuid4()), "type": "workout.updated"}
    with (
        patch.object(task, "SessionLocal") as session_local,
        patch.object(task, "ProviderFactory") as factory,
        patch.object(task, "_emit_webhook_sync_status") as sync_log,
        patch.object(handler.workouts, "get_workout_detail_from_api", return_value=workout_fixture(payload["id"])),
        patch.object(handler.workouts, "save_single_workout", side_effect=RuntimeError("synthetic write failure")),
        patch.object(task.process_webhook_push, "retry") as retry,
    ):
        session_local.return_value.__enter__.return_value = db
        factory.return_value.get_provider.return_value.webhooks = handler
        task.process_webhook_push.push_request(retries=task.process_webhook_push.max_retries)
        try:
            with pytest.raises(RuntimeError, match="synthetic write failure"):
                task.process_webhook_push.run("whoop", payload, TRACE_ID)
        finally:
            task.process_webhook_push.pop_request()
        retry.assert_not_called()
        assert {c.args[1]["user_id"] for c in sync_log.call_args_list} == {str(c.user_id) for c in connections}
        assert all(c.args[1]["status"] == "error" for c in sync_log.call_args_list)
