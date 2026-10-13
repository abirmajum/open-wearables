"""Whoop webhook handler.

Whoop sends notify-only webhooks: a lightweight payload containing the resource
ID and event type. The actual data must be fetched via the REST API.

Signature scheme
----------------
  Message  : timestamp_ms_string + raw_request_body
  Algorithm: HMAC-SHA256(client_secret, message)
  Encoding : base64
  Headers  : X-WHOOP-Signature, X-WHOOP-Signature-Timestamp

The endpoint must respond quickly; ``dispatch()`` stores the raw payload and
enqueues a Celery task, returning 200 immediately. ``process_payload()`` does
the actual API fetch and DB write, called by the task.

Linked profiles
---------------
One WHOOP account may be connected to several OW users. ``process_payload()``
fetches the resource once and saves it for every active linked connection, so
each profile gets its record (and outgoing ``workout.created`` / ``sleep.created``
events) from the webhook itself. Each profile is processed independently.

Supported event types
---------------------
  workout.updated / workout.deleted
  sleep.updated   / sleep.deleted
  recovery.updated / recovery.deleted

See: https://developer.whoop.com/docs/developing/webhooks/
"""

import base64
import hashlib
import hmac
import json
import logging
import time
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any
from uuid import UUID, uuid4

import httpx
from celery import current_app as celery_app
from fastapi import HTTPException, Request
from pydantic import ValidationError
from redis.exceptions import RedisError
from sqlalchemy.exc import DBAPIError, OperationalError

from app.config import settings
from app.database import DbSession
from app.models import UserConnection
from app.repositories import UserConnectionRepository
from app.schemas.providers.whoop import WhoopWebhookNotification, WhoopWebhookNotificationType
from app.services.providers.templates.base_webhook_handler import BaseWebhookHandler
from app.services.providers.whoop.data_247 import Whoop247Data
from app.services.providers.whoop.workouts import WhoopWorkouts
from app.services.raw_payload_storage import store_raw_payload
from app.utils.sentry_helpers import log_and_capture_error
from app.utils.structured_logging import log_structured

logger = logging.getLogger(__name__)

_PROCESS_PUSH_TASK = "app.integrations.celery.tasks.webhook_push_task.process_webhook_push"

# Fetch errors that would repeat with every linked profile's token: the resource is
# gone, or the app-wide rate limit is exhausted.
_SHARED_FETCH_ERROR_STATUSES = frozenset({404, 410, 429})

# WHOOP responses worth retrying: token refresh, timeouts and the rate limit.
_TRANSIENT_HTTP_STATUSES = frozenset({401, 408, 429})


def _is_transient(exc: Exception) -> bool:
    """True for failures a later retry can fix: infrastructure, network or WHOOP-side errors."""
    if isinstance(exc, HTTPException):
        return exc.status_code >= 500 or exc.status_code in _TRANSIENT_HTTP_STATUSES
    if isinstance(exc, DBAPIError):
        return isinstance(exc, OperationalError) or exc.connection_invalidated
    return isinstance(exc, TimeoutError | ConnectionError | httpx.TransportError | RedisError)


@dataclass(frozen=True)
class _FetchedResource:
    """A notification's WHOOP payloads, fetched once and saved for each linked profile."""

    record: dict[str, Any]
    cycles: list[dict[str, Any]] = field(default_factory=list)


class WhoopWebhookHandler(BaseWebhookHandler):
    """Webhook handler for Whoop notify-only events."""

    user_id_field = "user_id"

    def __init__(self, data_247: Whoop247Data, workouts: WhoopWorkouts) -> None:
        super().__init__("whoop")
        self.data_247 = data_247
        self.workouts = workouts
        self.connection_repo = UserConnectionRepository()

    # ------------------------------------------------------------------
    # BaseWebhookHandler interface
    # ------------------------------------------------------------------

    def verify_signature(self, request: Request, body: bytes) -> bool:
        """Verify X-WHOOP-Signature using HMAC-SHA256 + base64."""
        secret_setting = settings.whoop_client_secret
        if not secret_setting:
            log_structured(
                logger,
                "error",
                "WHOOP_CLIENT_SECRET not configured; rejecting webhook",
                provider="whoop",
                action="webhook_signature_missing_secret",
            )
            return False

        signature = request.headers.get("X-WHOOP-Signature")
        timestamp = request.headers.get("X-WHOOP-Signature-Timestamp")

        if not signature or not timestamp:
            log_structured(
                logger,
                "warning",
                "Missing Whoop webhook signature headers",
                provider="whoop",
                action="webhook_signature_missing",
                has_signature=bool(signature),
                has_timestamp=bool(timestamp),
            )
            return False

        try:
            ts_seconds = int(timestamp) / 1000
        except ValueError:
            log_structured(
                logger,
                "warning",
                "Unparsable X-WHOOP-Signature-Timestamp",
                provider="whoop",
                action="webhook_signature_timestamp_unparsable",
                timestamp=timestamp,
            )
            return False

        if abs(time.time() - ts_seconds) > 300:
            log_structured(
                logger,
                "warning",
                "Stale Whoop webhook timestamp",
                provider="whoop",
                action="webhook_signature_stale",
                timestamp=timestamp,
            )
            return False

        secret = secret_setting.get_secret_value()
        mac = hmac.new(secret.encode(), timestamp.encode() + body, hashlib.sha256)
        expected = base64.b64encode(mac.digest()).decode()
        return hmac.compare_digest(expected, signature)

    def parse_payload(self, body: bytes) -> dict[str, Any]:
        try:
            return json.loads(body)
        except (json.JSONDecodeError, ValueError) as exc:
            raise HTTPException(status_code=400, detail="Invalid JSON body") from exc

    def dispatch(self, db: DbSession, payload: dict[str, Any]) -> dict[str, Any]:
        """Store the raw payload and enqueue async processing. Returns immediately."""
        request_trace_id = str(uuid4())[:8]
        event_type = payload.get("type", "unknown")
        whoop_user_id = payload.get("user_id", "unknown")

        log_structured(
            logger,
            "info",
            "Received Whoop webhook",
            provider="whoop",
            trace_id=request_trace_id,
            event_type=event_type,
            whoop_user_id=whoop_user_id,
        )

        store_raw_payload(source="webhook", provider="whoop", payload=payload, trace_id=request_trace_id)

        task = celery_app.send_task(_PROCESS_PUSH_TASK, args=["whoop", payload, request_trace_id], queue="webhook_sync")
        log_structured(
            logger,
            "info",
            "Enqueued Whoop webhook processing task",
            provider="whoop",
            trace_id=request_trace_id,
            task_id=getattr(task, "id", None),
        )

        return {"status": "accepted"}

    def supported_event_types(self) -> list[str]:
        return list(WhoopWebhookNotificationType)

    # ------------------------------------------------------------------
    # Async processing (called by Celery task)
    # ------------------------------------------------------------------

    def process_payload(self, db: DbSession, payload: dict[str, Any], trace_id: str) -> dict[str, Any]:
        """Process a Whoop notify-only payload synchronously.

        Called by the ``process_webhook_push`` Celery task with its own DB session.

        One WHOOP account can be linked to several OW profiles. The resource is fetched
        once and saved for every active linked profile, so each profile gets its record
        and outgoing events immediately rather than on the next pull. Profiles are
        processed independently; the task retries only when every profile failed.
        """
        try:
            notification = WhoopWebhookNotification(**payload)
        except (ValidationError, TypeError) as exc:
            return {"status": "error", "error": f"Invalid payload: {exc}"}

        connections = self.connection_repo.get_all_by_provider_user_id(db, "whoop", str(notification.user_id))
        if not connections:
            log_structured(
                logger,
                "warning",
                "No connection found for Whoop user",
                provider="whoop",
                trace_id=trace_id,
                whoop_user_id=notification.user_id,
                event_type=notification.type,
            )
            return {"status": "user_not_found", "whoop_user_id": notification.user_id, "event_type": notification.type}

        user_ids = [connection.user_id for connection in connections]
        resource_id = str(notification.id)

        log_structured(
            logger,
            "info",
            "Processing Whoop webhook notification",
            provider="whoop",
            trace_id=trace_id,
            user_ids=[str(user_id) for user_id in user_ids],
            linked_profiles=len(user_ids),
            whoop_user_id=notification.user_id,
            event_type=notification.type,
            resource_id=resource_id,
        )

        event_type = notification.type
        handle: Callable[[UUID], dict[str, Any]]
        if event_type.is_delete_type:

            def handle(user_id: UUID) -> dict[str, Any]:
                return self._handle_deleted(db, event_type, user_id, resource_id)

        elif event_type.is_update_type:
            fetched = self._fetch_updated(db, event_type, connections, resource_id)
            if not isinstance(fetched, _FetchedResource):
                return {**fetched, "user_id": str(user_ids[0])}

            def handle(user_id: UUID) -> dict[str, Any]:
                return self._save_updated(db, event_type, user_id, fetched)

        else:
            log_structured(
                logger,
                "info",
                "Unhandled Whoop webhook event type",
                provider="whoop",
                trace_id=trace_id,
                event_type=event_type,
                user_ids=[str(user_id) for user_id in user_ids],
            )
            return {
                "status": "ignored",
                "reason": f"unhandled_event_type: {event_type}",
                "user_id": str(user_ids[0]),
            }

        return self._fan_out(db, connections, notification, trace_id, handle)

    def _fan_out(
        self,
        db: DbSession,
        connections: list[UserConnection],
        notification: WhoopWebhookNotification,
        trace_id: str,
        handle: Callable[[UUID], dict[str, Any]],
    ) -> dict[str, Any]:
        """Run ``handle`` for each linked profile, isolating failures per profile.

        Returns the first profile's result with every profile's result under ``profiles``.
        When every profile failed, a transient error (database, network, WHOOP 5xx/401/429)
        is raised so the Celery task retries. Deterministic errors would fail the same way
        on every retry and re-emit outgoing events, so they are returned, not raised.
        """
        profiles: list[dict[str, Any]] = []
        errors: list[Exception] = []
        for connection in connections:
            user_id = connection.user_id
            try:
                result = handle(user_id)
                self.connection_repo.update_last_synced_at(db, connection)
            except Exception as exc:
                db.rollback()
                errors.append(exc)
                log_and_capture_error(
                    exc,
                    logger,
                    "Failed to process Whoop webhook for linked profile",
                    extra={
                        "provider": "whoop",
                        "trace_id": trace_id,
                        "user_id": str(user_id),
                        "event_type": str(notification.type),
                    },
                )
                result = {"status": "error", "event_type": notification.type, "error": str(exc)}
            result["user_id"] = str(user_id)
            log_structured(
                logger,
                "warning" if result["status"] == "error" else "info",
                "Whoop webhook profile outcome",
                provider="whoop",
                action="whoop_webhook_profile_outcome",
                trace_id=trace_id,
                whoop_user_id=notification.user_id,
                user_id=str(user_id),
                event_type=notification.type,
                status=result["status"],
                records_saved=result.get("records_saved"),
                reason=result.get("reason"),
                error=result.get("error"),
            )
            profiles.append(result)

        if len(errors) == len(connections):
            transient = next((exc for exc in errors if _is_transient(exc)), None)
            if transient is not None:
                raise transient
            log_structured(
                logger,
                "error",
                "Whoop webhook failed for every linked profile with a non-retriable error",
                provider="whoop",
                action="whoop_webhook_failed_non_retriable",
                trace_id=trace_id,
                whoop_user_id=notification.user_id,
                event_type=notification.type,
                errors=[str(exc) for exc in errors],
            )
        return {**profiles[0], "profiles": profiles}

    # ------------------------------------------------------------------
    # Per-event-type handlers
    # ------------------------------------------------------------------

    def _fetch_with_fallback(
        self,
        db: DbSession,
        connections: list[UserConnection],
        fetch: Callable[[UUID], dict[str, Any]],
    ) -> tuple[UUID, dict[str, Any]]:
        """Fetch with the first linked profile whose token works.

        Every linked profile reads the same WHOOP account, so one fetch serves all of
        them. The most recently synced connection is tried first, since a stale profile's
        token is the one most likely to be broken; a failure falls through to the next
        profile. Errors about the resource or the app's rate limit would repeat for every
        token, so they are raised immediately. When every token fails, a transient error
        is preferred so the task retries.
        """
        never = datetime.min.replace(tzinfo=timezone.utc)
        ordered = sorted(
            connections,
            key=lambda c: (c.last_synced_at or never, c.created_at or never),
            reverse=True,
        )
        user_ids = [connection.user_id for connection in ordered]
        errors: list[Exception] = []
        for user_id in user_ids:
            try:
                return user_id, fetch(user_id)
            except HTTPException as exc:
                if exc.status_code in _SHARED_FETCH_ERROR_STATUSES:
                    raise
                errors.append(exc)
            except Exception as exc:
                errors.append(exc)
            # A failed token refresh can leave the session mid-transaction.
            db.rollback()
            log_structured(
                logger,
                "warning",
                "Whoop fetch failed for linked profile; trying the next one",
                provider="whoop",
                action="whoop_webhook_fetch_fallback",
                user_id=str(user_id),
                error=str(errors[-1]),
            )
        raise next((exc for exc in errors if _is_transient(exc)), errors[-1])

    def _fetch_updated(
        self,
        db: DbSession,
        event_type: WhoopWebhookNotificationType,
        connections: list[UserConnection],
        resource_id: str,
    ) -> _FetchedResource | dict[str, Any]:
        """Fetch the updated resource once. Returns a result dict when there is nothing to save."""
        match event_type:
            case WhoopWebhookNotificationType.WORKOUT_UPDATED:
                fetcher_id, raw = self._fetch_with_fallback(
                    db, connections, lambda user_id: self.workouts.get_workout_detail_from_api(db, user_id, resource_id)
                )
                store_raw_payload(
                    source="api_response",
                    provider="whoop",
                    payload=raw,
                    user_id=str(fetcher_id),
                    trace_id=f"/v2/activity/workout/{resource_id}",
                )
                return _FetchedResource(record=raw if isinstance(raw, dict) else {})
            case WhoopWebhookNotificationType.SLEEP_UPDATED:
                fetcher_id, sleep = self._fetch_with_fallback(
                    db, connections, lambda user_id: self.data_247.get_sleep_record(db, user_id, resource_id)
                )
                # WHOOP starts a cycle at sleep onset and emits no cycle event. Refresh
                # the sleep's cycle (normally the one now in progress) and the cycle it
                # followed, which closed at this sleep's onset, with one list request.
                cycles: list[dict[str, Any]] = []
                try:
                    cycles = self._fetch_cycles_for_sleep(db, fetcher_id, sleep)
                except Exception as exc:
                    # Best effort: the sleep still saves, and the periodic pull refreshes cycles.
                    db.rollback()
                    log_structured(
                        logger,
                        "warning",
                        "Failed to fetch Whoop cycles for sleep.updated",
                        provider="whoop",
                        action="whoop_webhook_cycle_fetch_failed",
                        user_id=str(fetcher_id),
                        resource_id=resource_id,
                        error=str(exc),
                    )
                return _FetchedResource(record=sleep, cycles=cycles)
            case WhoopWebhookNotificationType.RECOVERY_UPDATED:
                fetcher_id, sleep = self._fetch_with_fallback(
                    db, connections, lambda user_id: self.data_247.get_sleep_record(db, user_id, resource_id)
                )
                cycle_id = sleep.get("cycle_id")
                if not cycle_id:
                    log_structured(
                        logger,
                        "warning",
                        "No cycle_id found in sleep record for Whoop recovery.updated",
                        provider="whoop",
                        action="whoop_webhook_recovery_missing_cycle_id",
                        user_id=str(fetcher_id),
                        resource_id=resource_id,
                    )
                    return {
                        "status": "ignored",
                        "reason": "missing_cycle_id",
                        "resource_id": resource_id,
                    }
                recovery = self.data_247.get_recovery_record(db, fetcher_id, str(cycle_id))
                return _FetchedResource(record=recovery)
            case _:
                return {"status": "ignored", "reason": f"unhandled_event_type: {event_type}"}

    def _fetch_cycles_for_sleep(self, db: DbSession, user_id: UUID, sleep: dict[str, Any]) -> list[dict[str, Any]]:
        """Fetch the cycles of the two days ending at the sleep's end, plus its own cycle if missing."""
        end_time = datetime.now(timezone.utc)
        with suppress(AttributeError, TypeError, ValueError):
            end_time = datetime.fromisoformat(sleep["end"].replace("Z", "+00:00"))
        cycles = self.data_247.get_recent_cycles(db, user_id, end_time)
        cycle_id = sleep.get("cycle_id")
        if cycle_id and all(str(cycle.get("id")) != str(cycle_id) for cycle in cycles):
            cycles.append(self.data_247.get_cycle_record(db, user_id, str(cycle_id)))
        return cycles

    def _save_updated(
        self,
        db: DbSession,
        event_type: WhoopWebhookNotificationType,
        user_id: UUID,
        fetched: _FetchedResource,
    ) -> dict[str, Any]:
        """Save an already-fetched resource for one linked profile. Errors propagate."""
        count = 0
        match event_type:
            case WhoopWebhookNotificationType.WORKOUT_UPDATED:
                if fetched.record:
                    count = self.workouts.save_single_workout(db, user_id, fetched.record)
            case WhoopWebhookNotificationType.SLEEP_UPDATED:
                if fetched.record:
                    count = self.data_247.save_single_sleep(db, user_id, fetched.record)
                if fetched.cycles:
                    self._save_cycles(db, user_id, fetched.cycles)
            case WhoopWebhookNotificationType.RECOVERY_UPDATED:
                if fetched.record:
                    count = self.data_247.save_single_recovery(db, user_id, fetched.record)
        return {"status": "processed", "event_type": event_type, "records_saved": count}

    def _save_cycles(self, db: DbSession, user_id: UUID, cycles: list[dict[str, Any]]) -> None:
        """Save the sleep's cycles; a failure here must not fail the saved sleep."""
        try:
            self.data_247.save_cycles(db, user_id, cycles)
        except Exception as exc:
            db.rollback()
            log_structured(
                logger,
                "warning",
                "Failed to save Whoop cycles for sleep.updated",
                provider="whoop",
                action="whoop_webhook_cycle_save_failed",
                user_id=str(user_id),
                cycle_ids=[str(cycle.get("id")) for cycle in cycles],
                error=str(exc),
            )

    def _handle_deleted(
        self,
        db: DbSession,
        event_type: WhoopWebhookNotificationType,
        user_id: UUID,
        resource_id: str,
    ) -> dict[str, Any]:
        """Delete the EventRecord matching external_id.

        Recovery is stored as DataPointSeries (no external_id index), so
        recovery.deleted is logged but not actioned.
        """
        if event_type == WhoopWebhookNotificationType.RECOVERY_DELETED:
            log_structured(
                logger,
                "info",
                "Ignoring recovery.deleted (recovery stored as time-series, no external_id index)",
                provider="whoop",
                action="whoop_webhook_recovery_delete_skipped",
                user_id=str(user_id),
                resource_id=resource_id,
            )
            return {"status": "ignored", "reason": "recovery_delete_not_supported"}

        deleted = self.data_247.event_record_repo.delete_by_external_id(db, user_id, resource_id, source="whoop")
        if not deleted:
            log_structured(
                logger,
                "info",
                "No EventRecord found for deleted Whoop resource",
                provider="whoop",
                action="whoop_webhook_delete_not_found",
                event_type=event_type,
                user_id=str(user_id),
                resource_id=resource_id,
            )
            return {"status": "ignored", "reason": "record_not_found"}

        log_structured(
            logger,
            "info",
            "Deleted EventRecord for Whoop resource",
            provider="whoop",
            action="whoop_webhook_deleted",
            event_type=event_type,
            user_id=str(user_id),
            resource_id=resource_id,
        )
        return {"status": "deleted", "event_type": event_type, "resource_id": resource_id}
