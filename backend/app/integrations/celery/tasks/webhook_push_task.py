"""Process provider webhook payloads and notifications on their assigned Celery queues."""

from random import randint
from typing import Any
from uuid import UUID

from celery import Task, shared_task
from psycopg.errors import ForeignKeyViolation

from app.constants.celery_tasks import CeleryTask
from app.database import SessionLocal
from app.schemas.sync_status import SyncStatus
from app.services import sync_status_service
from app.services.providers.factory import ProviderFactory
from app.services.providers.sync_247_result import error_code_of
from app.services.providers.templates.base_webhook_handler import BaseWebhookHandler
from app.services.providers.whoop.webhook_handler import WhoopFanoutError, WhoopWebhookHandler
from app.utils.exceptions import (
    INTEGRITY_ERRORS,
    REAUTH_REQUIRED,
    AppError,
    ConfigurationError,
    ProviderError,
    ReauthRequiredError,
    describe_integrity_error,
    integrity_violation,
    is_provider_state_error,
    is_unique_violation,
)
from app.utils.structured_logging import get_logger

logger = get_logger(__name__)

# Upstream 4xx where retrying can't recover the object. 429 (rate limit) is excluded,
# it benefits from a retry; a 401 never gets here, it is a ProviderConnectionError.
_NONRETRIABLE_UPSTREAM_STATUSES = frozenset({400, 403, 404, 410, 422})

# Providers whose webhook deliveries are surfaced in the sync log via this task.
# Garmin self-reports sync status from its own handler/backfill, so it is excluded
# here to avoid duplicate run entries.
_SYNC_LOG_PROVIDERS = frozenset({"oura", "strava", "polar", "whoop", "suunto"})

# process_payload "status" values that mean data was actually persisted.
_SAVED_STATUSES = frozenset({"processed", "saved", "accepted", "deleted", "success"})

_RETRY_BASE_SECONDS = 30
_RETRY_CAP_SECONDS = 180


def _retry_countdown(retries: int) -> int:
    ceiling = min(_RETRY_BASE_SECONDS * 2**retries, _RETRY_CAP_SECONDS)
    # Jitter spreads a burst of failed deliveries without exhausting retries during a short outage.
    return randint(max(_RETRY_BASE_SECONDS, ceiling // 2), ceiling)


def _extract_item_count(result: dict[str, Any]) -> int | None:
    """Best-effort count of records persisted, across provider result shapes."""
    for key in ("records_saved", "saved_count", "items_processed", "processed_count", "record_count"):
        value = result.get(key)
        if isinstance(value, int):
            return value
    saved = result.get("saved")
    if isinstance(saved, int):
        return saved
    if isinstance(saved, dict):
        return sum(v for v in saved.values() if isinstance(v, int))
    return None


def _extract_breakdown(result: dict[str, Any], count: Any) -> dict[str, int] | None:
    """New-vs-updated split, derived from WriteCounts carried on the count or the saved dict."""
    inserted = getattr(count, "inserted", None)
    updated = getattr(count, "updated", None)
    if inserted is None and updated is None:
        saved = result.get("saved")
        if isinstance(saved, dict):
            members = [v for v in saved.values() if hasattr(v, "inserted")]
            if members:
                inserted = sum(v.inserted for v in members)
                updated = sum(v.updated for v in members)
    if inserted is None and updated is None:
        inserted = result.get("records_inserted")
        updated = result.get("records_updated")
    if inserted is None and updated is None:
        return None
    return {"inserted": inserted or 0, "updated": updated or 0}


def _emit_final_failure(
    provider_name: str,
    handler: BaseWebhookHandler | None,
    provider_user_id: str | None,
    exc: Exception,
    retry_user_ids: list[str] | None = None,
) -> None:
    """Record a delivery that ran out of retries as FAILED"""
    if handler is None or not provider_user_id:
        return
    try:
        if isinstance(handler, WhoopWebhookHandler) and retry_user_ids is not None:
            user_ids = retry_user_ids
        else:
            with SessionLocal() as db:
                if isinstance(handler, WhoopWebhookHandler):
                    connections = handler.connection_repo.get_all_by_provider_user_id(
                        db, "whoop", str(provider_user_id)
                    )
                else:
                    connection = handler.find_connection(db, str(provider_user_id))
                    connections = [connection] if connection else []
                user_ids = [str(c.user_id) for c in connections]
        for user_id in user_ids:
            if retry_user_ids is not None and user_id not in retry_user_ids:
                continue
            _emit_webhook_sync_status(
                provider_name,
                {"status": "error", "error": error_code_of(exc), "user_id": user_id},
            )
    except Exception as emit_exc:  # pragma: no cover - sync log must never break processing
        logger.debug("Failed to emit final webhook failure", provider=provider_name, error=str(emit_exc))


def _emit_webhook_sync_status(provider_name: str, result: Any) -> None:
    """Record a webhook delivery in the sync log (best-effort, never raises).

    Saves and errors become full run entries; ignored / duplicate / no-op
    deliveries are recorded as SKIPPED so the UI can filter them out.
    Deliveries that never resolved to a user (invalid payload, user_not_found)
    are skipped entirely — there is no user to attribute the run to.
    """
    try:
        if provider_name not in _SYNC_LOG_PROVIDERS or not isinstance(result, dict):
            return

        raw_user_id = result.get("user_id")
        if not raw_user_id:
            return
        try:
            user_id = UUID(str(raw_user_id))
        except (ValueError, TypeError):
            return

        status_str = str(result.get("status") or "").lower()
        count = _extract_item_count(result)
        descriptor = result.get("data_type") or result.get("event_type") or ""

        breakdown = _extract_breakdown(result, count)

        if status_str == "error":
            sync_status_service.emit_webhook_delivered(
                user_id,
                provider_name,
                status=SyncStatus.FAILED,
                error=str(result.get("error") or "unknown"),
                message=f"webhook {descriptor}".strip(),
            )
        elif status_str in _SAVED_STATUSES and (count or 0) > 0:
            # Prefer the new/updated split so an upsert-in-place reads as
            # "0 new, 3 updated" rather than looking like freshly arrived data.
            detail = f"{breakdown['inserted']} new, {breakdown['updated']} updated" if breakdown else descriptor
            sync_status_service.emit_webhook_delivered(
                user_id,
                provider_name,
                status=SyncStatus.SUCCESS,
                items_processed=count,
                message=f"webhook {detail}".strip(),
                metadata=breakdown,
            )
        else:
            # processed-but-no-data, ignored, duplicate, unknown_event_type, …
            reason = result.get("reason") or (descriptor if status_str in _SAVED_STATUSES else status_str)
            sync_status_service.emit_webhook_delivered(
                user_id,
                provider_name,
                status=SyncStatus.SKIPPED,
                items_processed=count,
                message=f"skipped: {reason}".strip(),
                metadata=breakdown,
            )
    except Exception as exc:  # pragma: no cover - sync log must never break processing
        logger.debug("Failed to emit webhook sync status", provider=provider_name, exc_info=exc)


def _is_client_error(exc: Exception) -> bool:
    """A 4xx domain error or an FK violation from our own writes (the user was deleted meanwhile): the same
    payload fails the same way on retry, so drop it like an upstream 4xx. A duplicate is a race that a retry
    resolves; a NOT NULL or CHECK violation is our bug and fails the task."""
    if isinstance(exc, INTEGRITY_ERRORS):
        return isinstance(integrity_violation(exc), ForeignKeyViolation)
    return isinstance(exc, AppError) and exc.is_client_error


def _error_attributes(exc: Exception) -> dict[str, str]:
    """What failed, never str(exc) or a traceback: an IntegrityError's message carries the payload's values."""
    if isinstance(exc, INTEGRITY_ERRORS):
        violation_type, constraint = describe_integrity_error(exc)
        return {"error_type": violation_type, "constraint": str(constraint)}
    return {"error_type": type(exc).__name__, "error": str(exc)}


def _retryable_profile_error(exc: Exception) -> bool:
    if isinstance(exc, ValueError | ConfigurationError) or _is_client_error(exc):
        return False
    if isinstance(exc, ProviderError):
        return exc.upstream_status not in _NONRETRIABLE_UPSTREAM_STATUSES
    return not isinstance(exc, INTEGRITY_ERRORS) or is_unique_violation(exc)


@shared_task(
    name=CeleryTask.PROCESS_WEBHOOK_PUSH,
    bind=True,
    acks_late=True,
    reject_on_worker_lost=True,
    max_retries=8,
)
def process_webhook_push(
    self: Task,
    provider_name: str,
    payload: dict[str, Any],
    request_trace_id: str,
    retry_user_ids: list[str] | None = None,
) -> dict[str, Any]:
    """Process an accepted webhook using the provider's handler and a fresh DB session.

    Celery reschedules the original payload on the same queue for retryable failures.
    """
    handler = None
    provider_user_id: str | None = None
    try:
        strategy = ProviderFactory().get_provider(provider_name)
        handler = strategy.webhooks
        if handler is None:
            raise ValueError(f"Provider '{provider_name}' has no webhook handler")
        provider_user_id = handler.extract_user_id(payload)
        with SessionLocal() as db:
            if isinstance(handler, WhoopWebhookHandler):
                if retry_user_ids is None and provider_user_id:
                    retry_user_ids = [
                        str(c.user_id)
                        for c in handler.connection_repo.get_all_by_provider_user_id(db, "whoop", provider_user_id)
                    ]
                result = handler.process_payload(db, payload, request_trace_id, retry_user_ids=retry_user_ids)
            else:
                result = handler.process_payload(db, payload, request_trace_id)
            # Bulk sample/score writes defer their commit to the caller, so closing the
            # session without this would silently discard them.
            db.commit()
        profiles = result.get("profiles")
        for profile_result in profiles if isinstance(profiles, list) else [result]:
            _emit_webhook_sync_status(provider_name, profile_result)
        return result
    except WhoopFanoutError as exc:
        retry_ids = [uid for uid, error in exc.errors.items() if _retryable_profile_error(error)]
        exhausted = self.request.retries >= self.max_retries
        for profile in exc.profiles:
            if profile["user_id"] not in retry_ids or exhausted:
                _emit_webhook_sync_status(provider_name, profile)
        if retry_ids:
            cause = exc.errors[retry_ids[0]]
            if exhausted:
                raise cause from exc
            raise self.retry(
                exc=cause,
                args=(),
                kwargs={
                    "provider_name": provider_name,
                    "payload": payload,
                    "request_trace_id": request_trace_id,
                    "retry_user_ids": retry_ids,
                },
                countdown=_retry_countdown(self.request.retries),
            ) from exc
        return {**exc.profiles[0], "profiles": exc.profiles}
    except (ValueError, ConfigurationError) as exc:
        # Unknown provider, missing handler or invalid settings won't recover on retry.
        logger.error(
            "Webhook push task aborted — configuration error",
            provider=provider_name,
            trace_id=request_trace_id,
            provider_user_id=provider_user_id,
            error=str(exc),
        )
        raise
    except ReauthRequiredError as exc:
        # The grant is dead (and already revoked): no retry can fetch anything until the
        # user reconnects, so fail the delivery instead of retrying it.
        logger.warning(
            "Webhook push task failed - reconnection required",
            provider=provider_name,
            trace_id=request_trace_id,
            provider_user_id=provider_user_id,
            error=exc.reason,
        )
        result = {"status": "error", "error": REAUTH_REQUIRED, "user_id": str(exc.user_id)}
        if isinstance(handler, WhoopWebhookHandler):
            _emit_final_failure(provider_name, handler, provider_user_id, exc, retry_user_ids)
        else:
            _emit_webhook_sync_status(provider_name, result)
        return result
    except ProviderError as exc:
        # Non-retriable upstream 4xx (deleted/unqueryable object): ack so the task
        # drops instead of retrying forever. 5xx, 429 and no response fall through to retry.
        # A rejected token is a ProviderConnectionError, dropped below like any client error.
        if exc.upstream_status in _NONRETRIABLE_UPSTREAM_STATUSES:
            # A request the provider rejected (400, 422) is most likely our bug: alert on it.
            logger.warning(
                "Webhook push task skipped — upstream non-retriable response",
                exc_info=exc if exc.request_rejected else None,
                alert=exc.request_rejected,
                provider=provider_name,
                trace_id=request_trace_id,
                provider_user_id=provider_user_id,
                upstream_status=exc.upstream_status,
                error=exc.detail,
            )
            return {
                "status": "skipped",
                "reason": "upstream_non_retriable",
                "upstream_status": exc.upstream_status,
            }
        if self.request.retries >= self.max_retries:
            # The payload is lost now. CeleryIntegration reports the error re-raised below, but
            # drops a provider's answer about the user's state (a throttle), so alert on that here.
            logger.warning(
                "Webhook push task failed upstream, giving up",
                exc_info=exc,
                alert=is_provider_state_error(exc),
                provider=provider_name,
                trace_id=request_trace_id,
                provider_user_id=provider_user_id,
                upstream_status=exc.upstream_status,
                attempt=self.request.retries,
            )
            _emit_final_failure(provider_name, handler, provider_user_id, exc, retry_user_ids)
            raise
        countdown = _retry_countdown(self.request.retries)
        logger.warning(
            "Webhook push task failed, scheduling retry",
            provider=provider_name,
            trace_id=request_trace_id,
            provider_user_id=provider_user_id,
            upstream_status=exc.upstream_status,
            error=exc.detail,
            attempt=self.request.retries,
            max_retries=self.max_retries,
            retry_delay_seconds=countdown,
        )
        raise self.retry(exc=exc, countdown=countdown) from exc
    except Exception as exc:
        if _is_client_error(exc):
            logger.warning(
                "Webhook push task skipped — non-retriable error",
                provider=provider_name,
                trace_id=request_trace_id,
                provider_user_id=provider_user_id,
                **_error_attributes(exc),
            )
            _emit_final_failure(provider_name, handler, provider_user_id, exc, retry_user_ids)
            return {"status": "skipped", "reason": "non_retriable", "error_type": type(exc).__name__}
        if isinstance(exc, INTEGRITY_ERRORS) and not is_unique_violation(exc):
            # Our bug, and the same payload violates the constraint again on every retry:
            # fail now, CeleryIntegration reports the raised exception to Sentry.
            logger.error(
                "Webhook push task failed — constraint violation",
                provider=provider_name,
                trace_id=request_trace_id,
                provider_user_id=provider_user_id,
                **_error_attributes(exc),
            )
            _emit_final_failure(provider_name, handler, provider_user_id, exc, retry_user_ids)
            raise
        if self.request.retries >= self.max_retries:
            logger.error(
                "Webhook push task failed, giving up",
                provider=provider_name,
                trace_id=request_trace_id,
                provider_user_id=provider_user_id,
                **_error_attributes(exc),
                attempt=self.request.retries,
            )
            _emit_final_failure(provider_name, handler, provider_user_id, exc, retry_user_ids)
            raise
        countdown = _retry_countdown(self.request.retries)
        logger.error(
            "Webhook push task failed, scheduling retry",
            provider=provider_name,
            trace_id=request_trace_id,
            provider_user_id=provider_user_id,
            **_error_attributes(exc),
            attempt=self.request.retries,
            max_retries=self.max_retries,
            retry_delay_seconds=countdown,
        )
        raise self.retry(exc=exc, countdown=countdown) from exc
