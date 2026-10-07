"""Mock-only cursor regressions; also runnable with --confcutdir=tests/unit/sync_cursor."""

from collections.abc import Iterator
from datetime import datetime, timezone
from importlib import import_module
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest

from app.services.providers.whoop.data_247 import Whoop247Data
from app.services.providers.whoop.workouts import WhoopWorkouts

sync_module = import_module("app.integrations.celery.tasks.sync_vendor_data_task")
USER_ID = uuid4()
CURSOR = datetime(2026, 10, 1, tzinfo=timezone.utc)


@pytest.fixture
def sync_context(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    connection = SimpleNamespace(provider="whoop", provider_user_id=None, last_synced_at=CURSOR)
    repo = MagicMock()
    repo.get_all_active_by_user.return_value = [connection]
    provider = SimpleNamespace(
        capabilities=SimpleNamespace(rest_pull=True, max_historical_days=None),
        workouts=MagicMock(),
        data_247=MagicMock(),
    )
    provider.workouts.load_data.return_value = 0
    provider.data_247.load_and_save_all.return_value = {"sleep_sessions_synced": 0}
    monkeypatch.setattr(sync_module, "SessionLocal", MagicMock())
    monkeypatch.setattr(sync_module, "UserConnectionRepository", lambda: repo)
    monkeypatch.setattr(sync_module, "ProviderSettingsRepository", lambda: SimpleNamespace(get_all=lambda db: {}))
    monkeypatch.setattr(sync_module, "ProviderFactory", lambda: SimpleNamespace(get_provider=lambda name: provider))
    monkeypatch.setattr(sync_module.settings, "pull_sync_lookback", None)
    for name in ("started", "progress", "completed", "failed", "log_and_capture_error"):
        monkeypatch.setattr(sync_module, name, MagicMock())
    return SimpleNamespace(connection=connection, repo=repo, provider=provider)


@pytest.mark.parametrize("workouts", [0, 1, True])
def test_complete_live_sync_advances_even_when_empty(sync_context: SimpleNamespace, workouts: int | bool) -> None:
    sync_context.provider.workouts.load_data.return_value = workouts
    result = sync_module.sync_vendor_data.run(str(USER_ID))
    sync_context.repo.update_last_synced_at.assert_called_once()
    assert result["providers_synced"]["whoop"]["success"] is True


@pytest.mark.parametrize("failure", ["workout_false", "workout_exception", "data_exception", "sleep_partial"])
def test_incomplete_live_sync_preserves_cursor(sync_context: SimpleNamespace, failure: str) -> None:
    if failure == "workout_false":
        sync_context.provider.workouts.load_data.return_value = False
    elif failure == "workout_exception":
        sync_context.provider.workouts.load_data.side_effect = RuntimeError("provider unavailable")
    elif failure == "data_exception":
        sync_context.provider.data_247.load_and_save_all.side_effect = RuntimeError("provider unavailable")
    else:
        sync_context.provider.data_247.load_and_save_all.return_value = {"sleep_partial": 1, "sleep_sessions_synced": 2}
    result = sync_module.sync_vendor_data.run(str(USER_ID))
    sync_context.repo.update_last_synced_at.assert_not_called()
    assert result["providers_synced"]["whoop"]["success"] is False


def test_retry_reuses_preserved_cursor_then_advances(sync_context: SimpleNamespace) -> None:
    sync_context.provider.workouts.load_data.side_effect = [RuntimeError("timeout"), 0]
    sync_module.sync_vendor_data.run(str(USER_ID))
    sync_context.repo.update_last_synced_at.assert_not_called()
    sync_module.sync_vendor_data.run(str(USER_ID))
    calls = sync_context.provider.workouts.load_data.call_args_list
    assert calls[0].kwargs == calls[1].kwargs
    assert CURSOR.isoformat() in calls[1].kwargs.values()
    sync_context.repo.update_last_synced_at.assert_called_once()


def test_historical_sync_never_advances(sync_context: SimpleNamespace) -> None:
    sync_module.sync_vendor_data.run(str(USER_ID), is_historical=True)
    sync_context.repo.update_last_synced_at.assert_not_called()


def test_linked_profile_waiting_for_primary_preserves_cursor(
    sync_context: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    sync_context.connection.provider_user_id = "shared-account"
    monkeypatch.setattr(sync_module, "try_become_primary", lambda *args, **kwargs: (False, "", uuid4()))
    sync_module.sync_vendor_data.run(str(USER_ID))
    sync_context.provider.workouts.load_data.assert_not_called()
    sync_context.repo.update_last_synced_at.assert_not_called()


@pytest.fixture
def whoop() -> Iterator[Whoop247Data]:
    provider = Whoop247Data("whoop", "https://example.invalid", MagicMock())
    with (
        patch.object(provider, "load_and_save_sleep", return_value=(0, False)),
        patch.object(provider, "load_and_save_recovery", return_value=(0, False)),
        patch.object(provider, "load_and_save_cycles", return_value=(0, False)),
        patch.object(provider, "load_and_save_body_measurement", return_value=0),
    ):
        yield provider


@pytest.mark.parametrize("kind", ["sleep", "recovery", "cycles", "body_measurement"])
def test_whoop_type_error_reaches_cursor_guard(sync_context: SimpleNamespace, whoop: Whoop247Data, kind: str) -> None:
    getattr(whoop, f"load_and_save_{kind}").side_effect = RuntimeError("provider unavailable")
    sync_context.provider.data_247 = whoop
    result = sync_module.sync_vendor_data.run(str(USER_ID))
    sync_context.repo.update_last_synced_at.assert_not_called()
    provider_result = result["providers_synced"]["whoop"]
    assert provider_result["success"] is False
    assert provider_result["params"]["data_247"][f"{kind}_partial"] == 1


@pytest.mark.parametrize("kind", ["sleep", "recovery", "cycles"])
def test_whoop_truncated_page_reaches_cursor_guard(
    sync_context: SimpleNamespace, whoop: Whoop247Data, kind: str
) -> None:
    getattr(whoop, f"load_and_save_{kind}").return_value = (3, True)
    sync_context.provider.data_247 = whoop
    result = sync_module.sync_vendor_data.run(str(USER_ID))
    sync_context.repo.update_last_synced_at.assert_not_called()
    assert result["providers_synced"]["whoop"]["success"] is False


@pytest.mark.parametrize("kind", ["recovery", "cycles"])
def test_whoop_record_failure_marks_partial(kind: str) -> None:
    provider = Whoop247Data("whoop", "https://example.invalid", MagicMock())
    normalizer = "normalize_cycle" if kind == "cycles" else "normalize_recovery"
    with (
        patch.object(provider, "_fetch_paginated", return_value=([{}], False)),
        patch.object(provider, normalizer, side_effect=ValueError("invalid record")),
    ):
        count, partial = getattr(provider, f"load_and_save_{kind}")(MagicMock(), USER_ID, CURSOR, CURSOR)
    assert count == 0
    assert partial is True


def test_body_measurement_fetch_failure_propagates() -> None:
    provider = Whoop247Data("whoop", "https://example.invalid", MagicMock())
    with (
        patch.object(provider, "_make_api_request", side_effect=RuntimeError("timeout")),
        pytest.raises(RuntimeError, match="timeout"),
    ):
        provider.get_body_measurement(MagicMock(), USER_ID)


def test_workout_later_page_failure_is_not_reported_as_success() -> None:
    provider = WhoopWorkouts(MagicMock(), MagicMock(), "whoop", "https://example.invalid", MagicMock())
    record = SimpleNamespace(score_state="SCORED", score=None)
    collection = SimpleNamespace(records=[record], next_token="page-two")
    with (
        patch.object(provider, "get_workouts_from_api", side_effect=[{}, RuntimeError("page two timeout")]),
        patch("app.services.providers.whoop.workouts.WhoopWorkoutCollectionJSON", return_value=collection),
        patch("app.services.providers.whoop.workouts.store_raw_payload"),
        patch.object(provider, "_normalize_workout", return_value=(MagicMock(), MagicMock(), None)),
        patch("app.services.providers.whoop.workouts.event_record_service") as records,
        pytest.raises(RuntimeError, match="page two timeout"),
    ):
        provider.load_data(MagicMock(), USER_ID)
    records.create.assert_called_once()
    records.create_detail.assert_called_once()


def test_all_failed_sync_emits_failed_status(sync_context: SimpleNamespace) -> None:
    sync_context.provider.workouts.load_data.side_effect = RuntimeError("workouts unavailable")
    sync_context.provider.data_247.load_and_save_all.side_effect = RuntimeError("sleep unavailable")
    result = sync_module.sync_vendor_data.run(str(USER_ID))
    sync_context.repo.update_last_synced_at.assert_not_called()
    assert result["providers_synced"]["whoop"]["success"] is False
    sync_module.failed.assert_called_once()
    sync_module.completed.assert_not_called()


def test_linked_fanout_advances_after_success(sync_context: SimpleNamespace) -> None:
    sync_context.connection.provider_user_id = "shared-account"
    sync_module.sync_vendor_data.run(str(USER_ID), _skip_linked_fan_out=True)
    sync_context.repo.update_last_synced_at.assert_called_once()
