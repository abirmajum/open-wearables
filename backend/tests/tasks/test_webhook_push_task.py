"""Tests for webhook → sync-log instrumentation in process_webhook_push.

The new-vs-updated split is derived centrally: a provider only has to return a
WriteCounts as its count, and the task surfaces the split — no per-provider
plumbing. Saves become SUCCESS runs, errors FAILED, no-ops SKIPPED, and
deliveries with no resolvable user (or from self-reporting Garmin) are dropped.
"""

import contextlib
from decimal import Decimal
from typing import Any
from unittest.mock import ANY, create_autospec, patch
from uuid import UUID, uuid4

import pytest
from celery.exceptions import Retry
from psycopg.errors import CheckViolation, ForeignKeyViolation, NotNullViolation, UniqueViolation
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session

import app.integrations.celery.tasks.webhook_push_task as task
from app.constants.celery_tasks import CeleryQueue, CeleryTask
from app.models import DataSource, SeriesAggregate, UserConnection
from app.repositories.data_point_series_repository import WriteCounts
from app.schemas.sync_status import SyncStatus
from app.services.providers.oura.data_247 import Oura247Data
from app.services.providers.oura.strategy import OuraStrategy
from app.services.providers.templates.base_webhook_handler import BaseWebhookHandler
from app.utils.exceptions import (
    AppError,
    ConfigurationError,
    NotFoundError,
    ProviderAuthRejectedError,
    ProviderError,
    ProviderNotConnectedError,
    ProviderRateLimitError,
    ReauthRequiredError,
)
from tests.factories import UserConnectionFactory


@pytest.fixture
def user_id() -> str:
    return str(uuid4())


def _capture() -> tuple[list[dict], Any]:
    calls: list[dict] = []

    def fake(uid: Any, provider: str, **kw: Any) -> None:
        calls.append({"provider": provider, "user_id": str(uid), **kw})

    return calls, fake


def test_writecounts_split_surfaces_without_explicit_keys(user_id: str) -> None:
    """A WriteCounts count yields the new/updated split centrally (no records_inserted keys)."""
    calls, fake = _capture()
    with patch.object(task.sync_status_service, "emit_webhook_delivered", autospec=True, side_effect=fake):
        task._emit_webhook_sync_status(
            "oura",
            {
                "status": "processed",
                "data_type": "daily_activity",
                "records_saved": WriteCounts(0, 3),
                "user_id": user_id,
            },
        )
    assert len(calls) == 1
    c = calls[0]
    assert c["status"] == SyncStatus.SUCCESS
    assert c["items_processed"] == 3
    assert c["metadata"] == {"inserted": 0, "updated": 3}
    assert c["message"] == "webhook 0 new, 3 updated"


def test_plain_int_count_has_no_breakdown(user_id: str) -> None:
    """Workout-style providers returning a plain int still log a success run, no split."""
    calls, fake = _capture()
    with patch.object(task.sync_status_service, "emit_webhook_delivered", autospec=True, side_effect=fake):
        task._emit_webhook_sync_status("strava", {"status": "processed", "records_saved": 2, "user_id": user_id})
    assert calls[0]["status"] == SyncStatus.SUCCESS
    assert calls[0]["items_processed"] == 2
    assert calls[0]["metadata"] is None


@pytest.mark.parametrize(
    ("result", "expected"),
    [
        ({"status": "error", "error": "boom"}, SyncStatus.FAILED),
        ({"status": "ignored", "reason": "duplicate_activity"}, SyncStatus.SKIPPED),
        ({"status": "processed", "records_saved": 0}, SyncStatus.SKIPPED),  # saved nothing
    ],
)
def test_status_mapping(result: dict, expected: SyncStatus, user_id: str) -> None:
    calls, fake = _capture()
    with patch.object(task.sync_status_service, "emit_webhook_delivered", autospec=True, side_effect=fake):
        task._emit_webhook_sync_status("oura", {**result, "user_id": user_id})
    assert calls[0]["status"] == expected


def test_no_user_resolved_is_dropped() -> None:
    """user_not_found / invalid payloads carry no user_id → nothing to attribute."""
    calls, fake = _capture()
    with patch.object(task.sync_status_service, "emit_webhook_delivered", autospec=True, side_effect=fake):
        task._emit_webhook_sync_status("oura", {"status": "user_not_found", "oura_user_id": "x"})
    assert calls == []


def test_self_reporting_provider_excluded(user_id: str) -> None:
    """Garmin reports its own sync status; the shared task must not duplicate it."""
    calls, fake = _capture()
    with patch.object(task.sync_status_service, "emit_webhook_delivered", autospec=True, side_effect=fake):
        task._emit_webhook_sync_status("garmin", {"status": "processed", "saved_count": 9, "user_id": user_id})
    assert calls == []


def test_emission_never_raises() -> None:
    """Sync-log emission must never break webhook processing."""
    calls, fake = _capture()
    with patch.object(task.sync_status_service, "emit_webhook_delivered", autospec=True, side_effect=fake):
        task._emit_webhook_sync_status("oura", None)
        task._emit_webhook_sync_status("oura", {"status": "processed", "records_saved": 1, "user_id": "not-a-uuid"})
    assert calls == []


def _run_with_failing_handler(exc: Exception) -> dict[str, Any]:
    handler = create_autospec(BaseWebhookHandler, instance=True)
    handler.extract_user_id.return_value = "provider-user"
    handler.process_payload.side_effect = exc
    with (
        patch.object(task, "ProviderFactory", autospec=True) as factory,
        patch.object(task, "SessionLocal", autospec=True),
        patch.object(task.process_webhook_push, "retry", autospec=True, side_effect=Retry()),
    ):
        factory.return_value.get_provider.return_value.webhooks = handler
        return task.process_webhook_push.run("oura", {}, "trace-1")


@pytest.mark.parametrize(
    "exc",
    [
        IntegrityError("stmt", {}, ForeignKeyViolation("user deleted meanwhile")),
        ForeignKeyViolation("raw psycopg, from a COPY"),
        NotFoundError("User not found."),
    ],
)
def test_own_client_errors_are_dropped_not_retried(exc: Exception) -> None:
    result = _run_with_failing_handler(exc)

    assert result == {"status": "skipped", "reason": "non_retriable", "error_type": type(exc).__name__}


@pytest.mark.parametrize(
    "exc",
    [
        AppError("broken invariant"),
        RuntimeError("database down"),
        IntegrityError("INSERT ...", {"heart_rate": 187}, UniqueViolation("two pushes raced")),
        ProviderAuthRejectedError("oura"),
    ],
)
def test_server_errors_are_retried(exc: Exception) -> None:
    with pytest.raises(Retry):
        _run_with_failing_handler(exc)


@pytest.mark.parametrize("upstream_status", [400, 403, 404, 410, 422])
def test_upstream_client_errors_are_dropped_not_retried(upstream_status: int) -> None:
    """A deleted or unqueryable object at the provider fails the same way on every attempt."""
    result = _run_with_failing_handler(ProviderError("oura", upstream_status=upstream_status))

    assert result == {"status": "skipped", "reason": "upstream_non_retriable", "upstream_status": upstream_status}


@pytest.mark.parametrize(
    "exc",
    [
        ProviderError("oura", upstream_status=500),
        ProviderError("oura"),  # no response: timeout, connection reset
        ProviderRateLimitError("oura", upstream_status=429),
    ],
)
def test_upstream_outages_are_retried(exc: ProviderError) -> None:
    with pytest.raises(Retry):
        _run_with_failing_handler(exc)


@pytest.mark.parametrize("outage", ["database", "provider"])
def test_webhook_persists_after_a_six_minute_outage(db: Session, outage: str) -> None:
    provider_user_id = "oura-outage-test"
    connection = UserConnectionFactory(provider="oura", provider_user_id=provider_user_id)
    payload = {
        "event_type": "create",
        "data_type": "daily_spo2",
        "user_id": provider_user_id,
        "object_id": "spo2-1",
    }
    elapsed = 0
    recovery_time = 6 * 60
    db_connection = db.connection()

    def open_session() -> Session:
        if outage == "database" and elapsed < recovery_time:
            raise OperationalError(None, None, OSError("database unavailable"))
        return Session(bind=db_connection, join_transaction_mode="create_savepoint")

    def fetch(*args: Any) -> dict[str, Any]:
        if outage == "provider" and elapsed < recovery_time:
            raise ProviderError("oura", upstream_status=503)
        return {"id": "spo2-1", "day": "2026-01-15", "spo2_percentage": {"average": 97}}

    handler = OuraStrategy().webhooks
    job = task.process_webhook_push
    task_id = "accepted-webhook"
    with (
        patch("app.services.providers.oura.webhook_handler.celery_app") as celery,
        patch.object(job, "apply_async") as publish_retry,
        patch.object(task, "SessionLocal", side_effect=open_session),
        patch.object(Oura247Data, "_make_api_request", side_effect=fetch),
        patch.object(task, "randint", side_effect=lambda lower, upper: lower),
        patch.object(task.sync_status_service, "emit_webhook_delivered"),
    ):
        celery.send_task.return_value.id = task_id
        assert handler.dispatch(db, payload) == {"status": "accepted"}
        celery.send_task.assert_called_once_with(
            CeleryTask.PROCESS_WEBHOOK_PUSH, args=["oura", payload, ANY], queue=CeleryQueue.WEBHOOK_SYNC
        )
        args = tuple(celery.send_task.call_args.kwargs["args"])
        original_args = args
        retries = 0
        queue = CeleryQueue.WEBHOOK_SYNC
        while True:
            publish_retry.reset_mock()
            job.push_request(
                id=task_id,
                args=args,
                kwargs={},
                retries=retries,
                called_directly=False,
                delivery_info={"exchange": "", "routing_key": queue},
            )
            try:
                result = job.run(*args)
            except Retry:
                publish_retry.assert_called_once()
                args, _ = publish_retry.call_args.args
                options = publish_retry.call_args.kwargs
                assert args == original_args
                assert options["queue"] == CeleryQueue.WEBHOOK_SYNC
                assert options["retries"] == retries + 1
                assert 30 <= options["countdown"] <= 180
                retries = options["retries"]
                queue = options["queue"]
                elapsed += options["countdown"]
            else:
                publish_retry.assert_not_called()
                break
            finally:
                job.pop_request()

    assert elapsed >= recovery_time
    assert retries > 3
    assert result["status"] == "processed"
    stored = db.scalars(
        select(SeriesAggregate.avg)
        .join(DataSource, SeriesAggregate.data_source_id == DataSource.id)
        .where(DataSource.user_id == connection.user_id, DataSource.provider == "oura")
    ).one()
    assert stored == Decimal("97")


@pytest.mark.parametrize(("upstream_status", "alerted"), [(400, True), (422, True), (404, False), (410, False)])
def test_only_a_request_the_provider_refused_alerts_when_skipped(upstream_status: int, alerted: bool) -> None:
    """A 400 or 422 is most likely our bug; a deleted object at the provider is not."""
    with patch.object(task, "logger", autospec=True) as logger:
        _run_with_failing_handler(ProviderError("oura", upstream_status=upstream_status))

    assert logger.warning.call_args.kwargs["alert"] is alerted


@pytest.mark.parametrize(
    ("exc", "alerted"),
    [(ProviderError("oura", upstream_status=503), False), (ProviderRateLimitError("oura", upstream_status=429), True)],
)
def test_last_failed_attempt_reports_that_the_payload_is_lost(exc: ProviderError, alerted: bool) -> None:
    """CeleryIntegration reports the error the last attempt re-raises, except a throttle,
    which before_send drops: the line alerts only on that, so nothing is reported twice."""
    task.process_webhook_push.push_request(retries=task.process_webhook_push.max_retries)
    try:
        with patch.object(task, "logger", autospec=True) as logger, pytest.raises(type(exc)):
            _run_with_failing_handler(exc)
    finally:
        task.process_webhook_push.pop_request()

    assert logger.warning.call_args.args[0] == "Webhook push task failed upstream, giving up"
    assert logger.warning.call_args.kwargs["alert"] is alerted


def test_earlier_failed_attempts_do_not_alert() -> None:
    with patch.object(task, "logger", autospec=True) as logger, pytest.raises(Retry):
        _run_with_failing_handler(ProviderError("oura", upstream_status=503))

    assert logger.warning.call_args.args[0] == "Webhook push task failed, scheduling retry"
    assert "alert" not in logger.warning.call_args.kwargs


@pytest.mark.parametrize(
    "exc",
    [
        ProviderNotConnectedError("oura"),
    ],
)
def test_unusable_connection_is_dropped_not_retried(exc: Exception) -> None:
    """No retry gets past a revoked grant or a missing connection: only the user can fix it."""
    result = _run_with_failing_handler(exc)

    assert result == {"status": "skipped", "reason": "non_retriable", "error_type": type(exc).__name__}


def test_configuration_error_fails_without_retry() -> None:
    with pytest.raises(ConfigurationError):
        _run_with_failing_handler(ConfigurationError("missing credentials"))


@pytest.mark.parametrize(
    ("exc", "violation"),
    [
        (IntegrityError("INSERT ...", {"heart_rate": 187}, NotNullViolation("not null")), "NotNullViolation"),
        (CheckViolation("raw psycopg, from a COPY"), "CheckViolation"),
    ],
)
def test_own_constraint_bug_fails_without_retry(exc: Exception, violation: str) -> None:
    """Retrying the same payload violates the constraint again; raising gets it to Sentry."""
    with patch.object(task, "logger", autospec=True) as logger, pytest.raises(type(exc)):
        _run_with_failing_handler(exc)

    kwargs = logger.error.call_args.kwargs
    assert kwargs["error_type"] == violation
    assert "187" not in str(kwargs)


@pytest.mark.parametrize(
    "exc",
    [
        NotFoundError("User not found."),
        IntegrityError("INSERT ...", {}, NotNullViolation("not null")),
    ],
)
def test_a_delivery_given_up_on_is_recorded_as_failed(exc: Exception) -> None:
    """Dropped or failed without retry, the delivery still shows in the sync history."""
    with patch.object(task, "_emit_final_failure", autospec=True) as final_failure, contextlib.suppress(type(exc)):
        _run_with_failing_handler(exc)

    final_failure.assert_called_once()
    assert final_failure.call_args.args[-1] is exc


def test_skipped_integrity_error_is_logged_without_the_payloads_values() -> None:
    """The message of an IntegrityError carries the statement's parameters, i.e. health data."""
    exc = IntegrityError("INSERT ...", {"heart_rate": 187}, ForeignKeyViolation("fk"))

    with patch.object(task, "logger", autospec=True) as logger:
        _run_with_failing_handler(exc)

    kwargs = logger.warning.call_args.kwargs
    assert kwargs["error_type"] == "ForeignKeyViolation"
    assert "exc_info" not in kwargs
    assert "187" not in str(kwargs)


def test_reauth_required_fails_delivery_without_retry(user_id: str) -> None:
    """A dead grant raised out of the handler is logged FAILED and not retried."""
    calls, fake = _capture()
    handler = create_autospec(BaseWebhookHandler, instance=True)
    handler.extract_user_id.return_value = "athlete-1"
    handler.process_payload.side_effect = ReauthRequiredError("strava", UUID(user_id), "reconnection required")
    with (
        patch.object(task, "SessionLocal", autospec=True),
        patch.object(task, "ProviderFactory", autospec=True) as factory,
        patch.object(task.process_webhook_push, "retry", autospec=True) as retry,
        patch.object(task.sync_status_service, "emit_webhook_delivered", autospec=True, side_effect=fake),
    ):
        factory.return_value.get_provider.return_value.webhooks = handler
        result = task.process_webhook_push.run("strava", {}, "trace")

    retry.assert_not_called()
    assert result == {"status": "error", "error": "reauth_required", "user_id": user_id}
    assert calls[0]["status"] == SyncStatus.FAILED


@pytest.mark.parametrize(("retries", "expect_failed"), [(3, False), (task.process_webhook_push.max_retries, True)])
def test_failed_run_logged_only_when_retries_run_out(retries: int, expect_failed: bool, user_id: str) -> None:
    """A retriable error logs nothing until the last attempt, which is logged FAILED."""
    calls, fake = _capture()
    handler = create_autospec(BaseWebhookHandler, instance=True)
    handler.extract_user_id.return_value = "athlete-1"
    handler.process_payload.side_effect = RuntimeError("upstream timeout")
    handler.find_connection.return_value = UserConnection(user_id=UUID(user_id))
    with (
        patch.object(task, "SessionLocal", autospec=True),
        patch.object(task, "ProviderFactory", autospec=True) as factory,
        patch.object(task.process_webhook_push, "retry", autospec=True, side_effect=Retry()) as retry,
        patch.object(task.sync_status_service, "emit_webhook_delivered", autospec=True, side_effect=fake),
    ):
        factory.return_value.get_provider.return_value.webhooks = handler
        task.process_webhook_push.push_request(retries=retries)
        try:
            with pytest.raises(RuntimeError if expect_failed else Retry):
                task.process_webhook_push.run("strava", {}, "trace")
        finally:
            task.process_webhook_push.pop_request()

    if expect_failed:
        retry.assert_not_called()
        handler.find_connection.assert_called_once_with(ANY, "athlete-1")
        assert calls[0]["status"] == SyncStatus.FAILED
        assert calls[0]["error"] == "RuntimeError"
    else:
        retry.assert_called_once()
        assert calls == []
