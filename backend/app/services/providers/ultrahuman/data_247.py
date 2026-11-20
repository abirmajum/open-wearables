"""Ultrahuman Ring Air 24/7 data implementation for sleep, recovery, and activity samples."""

import math
import re
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, InvalidOperation
from enum import Enum
from typing import Any, TypeIs
from uuid import UUID, uuid4

from sqlalchemy.exc import DataError, IntegrityError

from app.config import settings
from app.constants.sleep import SleepStageType
from app.database import DbSession
from app.integrations.redis_client import get_redis_client
from app.models import EventRecord
from app.repositories import EventRecordRepository, UserConnectionRepository
from app.repositories.data_point_series_repository import WriteCounts
from app.schemas.enums import EventCategory, HealthScoreCategory, ProviderName, SeriesType, daily_total_flag
from app.schemas.model_crud.activities.data_point_series import CalendarPeriod, TimeSeriesSampleCreate
from app.schemas.model_crud.activities.event_record import EventRecordCreate
from app.schemas.model_crud.activities.event_record_detail import EventRecordDetailCreate
from app.schemas.model_crud.activities.health_score import HealthScoreCreate, ScoreComponent
from app.schemas.model_crud.activities.sleep import SleepStage
from app.schemas.sync_status import SyncScope
from app.services.event_record_service import event_record_service
from app.services.health_score_service import health_score_service
from app.services.providers.api_client import get_valid_token, make_authenticated_request
from app.services.providers.sync_247_result import Sync247Result, Sync247Run, error_code_of
from app.services.providers.templates.base_247_data import Base247DataTemplate
from app.services.providers.templates.base_oauth import BaseOAuthTemplate
from app.services.providers.ultrahuman.coverage import (
    ACTIVITY_SAMPLE_SERIES,
    DAILY_SCALAR_SERIES,
    DAILY_SCORES,
    GLUCOSE_METRICS,
    SCORE_COMPONENTS,
    SLEEP_SCORE_LOCATIONS,
)
from app.services.providers.ultrahuman.oauth import fetch_provider_user_info
from app.services.raw_payload_storage import store_raw_payload
from app.services.timeseries_service import timeseries_service
from app.utils.context import sync_run_var
from app.utils.conversion import as_float, as_int, seconds_to_minutes
from app.utils.dates import offset_to_iso
from app.utils.exceptions import ProviderConnectionError, ProviderError, ProviderRateLimitError
from app.utils.structured_logging import get_logger
from app.utils.units import BloodGlucoseUnit, UnitConverter, parse_unit

logger = get_logger(__name__)

# Ultrahuman sleep_graph.data stage names → our canonical SleepStageType.
SLEEP_GRAPH_STAGE_MAP: dict[str, SleepStageType] = {
    "deep_sleep": SleepStageType.DEEP,
    "light_sleep": SleepStageType.LIGHT,
    "rem_sleep": SleepStageType.REM,
    "awake": SleepStageType.AWAKE,
}

# The UTC offsets in use run from -12:00 to +14:00.
_WESTMOST, _EASTMOST = timedelta(hours=-12), timedelta(hours=14)

# A live sync retries a day whose fetch failed (5xx, no response) only while the day is this young: an older one is
# given up and read as empty, so a day Ultrahuman never serves cannot hold the live cursor, and widen every later
# pull's window, for good. Backfills keep reporting such a day as failed: they do not move the cursor.
MAX_RETRIED_DAY_AGE = timedelta(days=7)


class UltrahumanBodyError(ProviderError):
    """Ultrahuman answered 200 with an error in its ``{data, error, status}`` envelope; ``upstream_status`` is the
    body's status (None when it names none)."""


class DayFailure(Enum):
    """What a day's failed fetch means for the run (``Ultrahuman247Data._day_failure``)."""

    FATAL = "fatal"  # the grant is unusable: raised, ending the run
    EMPTY = "empty"  # a day without data
    FAIL = "fail"  # an outage: the day fails, so a live sync fetches it again
    THROTTLED = "throttled"  # failed like an outage, and the walk ends
    SKIP = "skip"  # would fail the same way every run: skipped as empty, reported


# The account id is looked up at most this often per connection while a connect could not read it.
PROFILE_ATTEMPT_INTERVAL = timedelta(days=1)


def _profile_attempt_allowed(connection_id: UUID) -> bool:
    """Whether this connection may try /user_info now; claims the attempt. False when Redis can't be reached."""
    try:
        return bool(
            get_redis_client().set(
                f"ultrahuman:user_info_attempt:{connection_id}",
                1,
                nx=True,
                ex=int(PROFILE_ATTEMPT_INTERVAL.total_seconds()),
            )
        )
    except Exception as e:
        logger.warning(
            "Ultrahuman user profile attempt not claimed", provider="ultrahuman", error_code=error_code_of(e)
        )
        return False


def _utc_today() -> date:
    return datetime.now(timezone.utc).date()


def _is_historical_run() -> bool:
    run = sync_run_var.get()
    return run is not None and run.scope == SyncScope.HISTORICAL


def _body_status(value: Any) -> int | None:
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    if isinstance(value, str) and value.strip().isdigit():
        return int(value)
    return None


def _envelope_error(body: Any) -> tuple[bool, int | None]:
    """Whether an Ultrahuman ``{data, error, status}`` body reports an error, and the status it names."""
    if not isinstance(body, dict):
        return False, None
    status = _body_status(body.get("status"))
    return bool(body.get("error")) or (status is not None and status != 200), status


def _at(items_by_type: dict[str, Any], path: tuple[str, ...]) -> Any:
    """What a path from the metric type leads to, a step into a list taking its item of that ``type``; None where
    the day lacks it."""
    node: Any = items_by_type
    for key in path:
        if isinstance(node, list):
            node = next((item for item in node if isinstance(item, dict) and item.get("type") == key), None)
        else:
            node = node.get(key) if isinstance(node, dict) else None
    return node


def _value_at(items_by_type: dict[str, Any], path: tuple[str, ...]) -> Any:
    """The ``value`` of the object a component path leads to; None where the day lacks it."""
    return _at(items_by_type, (*path, "value"))


def _dicts(items: Any) -> list[dict[str, Any]]:
    """The objects of a list in a payload; anything else in it, or a payload that is not a list, gives none."""
    return [item for item in items if isinstance(item, dict)] if isinstance(items, list) else []


def _is_number(value: Any) -> TypeIs[int | float]:
    return isinstance(value, int | float) and not isinstance(value, bool) and math.isfinite(value)


def _utc(timestamp: Any) -> datetime | None:
    """A unix timestamp as a UTC datetime; None for anything that is not a number of a representable instant."""
    if not _is_number(timestamp):
        return None
    try:
        return datetime.fromtimestamp(timestamp, tz=timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None


def _zone_offset(day: date, metrics: list[dict[str, Any]]) -> str | None:
    """The user's UTC offset on ``day``: Ultrahuman starts each metric's day at the user's local midnight. None when
    no metric carries a day start that is a real offset from ``day``."""
    midnight = datetime.combine(day, time(), timezone.utc)
    for item in metrics:
        metric = item.get("object")
        day_start = _utc(metric.get("day_start_timestamp")) if isinstance(metric, dict) else None
        if day_start is None:
            continue
        offset = midnight - day_start
        if _WESTMOST <= offset <= _EASTMOST:
            return offset_to_iso(int(offset.total_seconds()))
    return None


def _in_mg_dl(value: Any, unit: Any) -> Any:
    """A glucose value in mg/dL: converted when its metric names mmol/L as its unit, as received otherwise. No
    Ultrahuman payload seen so far names a glucose unit, so the mg/dL default is unverified."""
    if _is_number(value) and isinstance(unit, str) and parse_unit(unit) is BloodGlucoseUnit.mmol_l:
        return float(UnitConverter.blood_glucose(Decimal(str(value)), BloodGlucoseUnit.mmol_l, BloodGlucoseUnit.mg_dl))
    return value


def _components(category: HealthScoreCategory, items_by_type: dict[str, Any]) -> dict[str, ScoreComponent]:
    components: dict[str, ScoreComponent] = {}
    for name, path in SCORE_COMPONENTS[category].items():
        value = _value_at(items_by_type, path)
        if path[0] in GLUCOSE_METRICS:
            value = _in_mg_dl(value, _at(items_by_type, (*path, "unit")))
        if value is not None:
            components[name] = ScoreComponent(value=value)
    return components


class Ultrahuman247Data(Base247DataTemplate):
    """Ultrahuman Ring Air implementation for 24/7 data (sleep, recovery, activity)."""

    def __init__(
        self,
        provider_name: str,
        api_base_url: str,
        oauth: BaseOAuthTemplate,
    ) -> None:
        super().__init__(provider_name, api_base_url, oauth)
        self.event_record_repo = EventRecordRepository(EventRecord)
        self.connection_repo = UserConnectionRepository()

    def _make_api_request(
        self,
        db: DbSession,
        user_id: UUID,
        endpoint: str,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        check_body_errors: bool = True,
    ) -> Any:
        """Make authenticated request to Ultrahuman API."""
        return make_authenticated_request(
            db=db,
            user_id=user_id,
            connection_repo=self.connection_repo,
            oauth=self.oauth,
            api_base_url=self.api_base_url,
            provider_name=self.provider_name,
            endpoint=endpoint,
            method="GET",
            params=params,
            headers=headers,
            check_body_errors=check_body_errors,
        )

    def _fetch_daily_metrics(
        self,
        db: DbSession,
        user_id: UUID,
        date: datetime,
    ) -> list[dict[str, Any]]:
        """Fetch all metrics for a specific day from Ultrahuman API.

        Returns:
            list[dict[str, Any]]: The day's metric items; empty when Ultrahuman answered with no data for the day.
                Items that are not objects are dropped, and a metric type nothing here reads (``workout``, or one a
                new scope adds) is kept but ignored downstream.

            A 200 whose ``{data, error, status}`` envelope reports a 404 is a day without data too.

        Raises:
            ProviderConnectionError: The connection can't be used (token rejected or missing).
            ProviderError: The request failed: a 401/403, a throttle that outlasted the client's backoff, a 5xx, no
                response, a request Ultrahuman refused or an answer that is not JSON.
            UltrahumanBodyError: A 200 whose envelope reports any other error.
        """
        date_str = date.strftime("%Y-%m-%d")
        response = self._make_api_request(
            db,
            user_id,
            "/user_data/metrics",
            params={"date": date_str},
            # Ultrahuman's envelope carries its own status, read here rather than as a generic 400.
            check_body_errors=False,
        )
        has_error, status = _envelope_error(response)
        if has_error:
            if status == 404:
                self._log_empty_day(user_id, date, "body_404")
                return []
            raise UltrahumanBodyError(self.provider_name, upstream_status=status, request_rejected=False)
        store_raw_payload(
            source="api_response",
            provider="ultrahuman",
            payload=response,
            user_id=str(user_id),
            trace_id=date_str,
        )
        data = response.get("data") if isinstance(response, dict) else None
        metrics = _dicts(data.get("metric_data")) if isinstance(data, dict) else []
        zone_offset = _zone_offset(date.date(), metrics)
        for item in metrics:
            item["date"] = date_str
            # Inject the day and its offset into the inner object for use in normalization
            if isinstance(item.get("object"), dict):
                item["object"]["ultrahuman_date"] = date_str
                item["object"]["ultrahuman_zone_offset"] = zone_offset
        return metrics

    # -------------------------------------------------------------------------
    # Sleep Data
    # -------------------------------------------------------------------------

    @staticmethod
    def _normalize_sleep_stages(raw_sleep: dict[str, Any]) -> list[SleepStage]:
        """Parse the ``sleep_graph.data`` interval timeline into canonical SleepStage objects.

        Ultrahuman returns timestamped stage transitions as
        ``{"start": <unix>, "end": <unix>, "type": "deep_sleep", ...}``. Unknown stage
        names fall back to ``SleepStageType.UNKNOWN``; intervals without a numeric start and end are skipped.
        """
        graph = raw_sleep.get("sleep_graph")
        intervals = _dicts(graph.get("data")) if isinstance(graph, dict) else []

        stages: list[SleepStage] = []
        for interval in intervals:
            start_time = _utc(interval.get("start"))
            end_time = _utc(interval.get("end"))
            if start_time is None or end_time is None:
                continue
            stages.append(
                SleepStage(
                    stage=SLEEP_GRAPH_STAGE_MAP.get(interval.get("type"), SleepStageType.UNKNOWN),
                    start_time=start_time,
                    end_time=end_time,
                )
            )

        stages.sort(key=lambda s: s.start_time)
        return stages

    def normalize_sleep(
        self,
        raw_sleep: dict[str, Any],
        user_id: UUID,
    ) -> dict[str, Any]:
        """Normalize Ultrahuman sleep data (from 'Sleep' type object) to our schema."""
        # Times are unix timestamps
        bedtime_start_ts = raw_sleep.get("bedtime_start")
        bedtime_end_ts = raw_sleep.get("bedtime_end")
        date_str = raw_sleep.get("ultrahuman_date")

        start_dt = _utc(bedtime_start_ts) if bedtime_start_ts else None
        end_dt = _utc(bedtime_end_ts) if bedtime_end_ts else None

        # Extract durations from quick_metrics
        # "quick_metrics": [{"type": "time_in_bed", "value": 27000}, ...]
        quick_metrics = {m.get("type"): m.get("value", 0) for m in _dicts(raw_sleep.get("quick_metrics"))}

        # Values are typically in seconds
        time_in_bed_seconds = as_int(quick_metrics.get("time_in_bed")) or 0

        # Extract sleep stages from sleep_stages array
        # "sleep_stages": [{"type": "deep_sleep", "stage_time": 3240}, ...]
        sleep_stages = {s.get("type"): s.get("stage_time") for s in _dicts(raw_sleep.get("sleep_stages"))}
        deep_seconds = as_int(sleep_stages.get("deep_sleep"))
        rem_seconds = as_int(sleep_stages.get("rem_sleep"))
        light_seconds = as_int(sleep_stages.get("light_sleep"))
        awake_seconds = as_int(sleep_stages.get("awake"))

        # Efficiency from quick_metrics (type: "sleep_efic")
        efficiency = quick_metrics.get("sleep_efic")
        if efficiency is None:
            efficiency = raw_sleep.get("sleep_efficiency")

        internal_id = uuid4()

        return {
            "id": internal_id,
            "user_id": user_id,
            "provider": self.provider_name,
            "timestamp": start_dt.isoformat() if start_dt else date_str,
            "start_time": start_dt,
            "end_time": end_dt,
            "duration_seconds": time_in_bed_seconds,
            "efficiency_percent": as_float(efficiency),
            "is_nap": False,  # Ultrahuman doesn't explicitly mark naps in this structure
            "stages": {
                "deep_seconds": deep_seconds,
                "light_seconds": light_seconds,
                "rem_seconds": rem_seconds,
                "awake_seconds": awake_seconds,
            },
            "stage_timestamps": self._normalize_sleep_stages(raw_sleep),
            "ultrahuman_date": date_str,
            "zone_offset": raw_sleep.get("ultrahuman_zone_offset"),
            "raw": raw_sleep,
        }

    def save_sleep_data(
        self,
        db: DbSession,
        user_id: UUID,
        normalized_sleep: dict[str, Any],
    ) -> UUID | None:
        """Save normalized sleep data to database as EventRecord with SleepDetails.

        Returns the stored record's id (a merge with an adjacent session keeps the merged one), None if skipped.
        """
        sleep_id = normalized_sleep["id"]
        start_dt = normalized_sleep.get("start_time")
        end_dt = normalized_sleep.get("end_time")

        if not start_dt or not end_dt:
            logger.warning(
                "Skipping sleep record: missing start/end time",
                provider="ultrahuman",
                task="save_sleep_data",
                sleep_id=str(sleep_id),
                user_id=str(user_id),
            )
            return None

        # Create EventRecord for sleep
        record = EventRecordCreate(
            id=sleep_id,
            category=EventCategory.SLEEP,
            type="sleep_session",
            source_name="Ultrahuman Ring Air",
            duration_seconds=normalized_sleep.get("duration_seconds"),
            start_datetime=start_dt,
            end_datetime=end_dt,
            zone_offset=normalized_sleep.get("zone_offset"),
            external_id=f"sleep-{normalized_sleep.get('ultrahuman_date')}",
            provider=self.provider_name,
            user_id=user_id,
        )

        # Create detail with sleep-specific fields
        stages = normalized_sleep.get("stages", {})
        deep_seconds = stages.get("deep_seconds")
        light_seconds = stages.get("light_seconds")
        rem_seconds = stages.get("rem_seconds")
        awake_minutes = seconds_to_minutes(stages.get("awake_seconds"))
        total_sleep_minutes = seconds_to_minutes(deep_seconds, light_seconds, rem_seconds)
        time_in_bed_minutes = normalized_sleep.get("duration_seconds", 0) // 60

        # If total sleep is 0 but we have duration, try to infer
        if not total_sleep_minutes and time_in_bed_minutes > 0:
            total_sleep_minutes = time_in_bed_minutes - (awake_minutes or 0)

        detail = EventRecordDetailCreate(
            record_id=sleep_id,
            sleep_total_duration_minutes=total_sleep_minutes,
            sleep_time_in_bed_minutes=time_in_bed_minutes,
            sleep_efficiency_score=Decimal(str(normalized_sleep.get("efficiency_percent", 0)))
            if normalized_sleep.get("efficiency_percent") is not None
            else None,
            sleep_deep_minutes=seconds_to_minutes(deep_seconds),
            sleep_light_minutes=seconds_to_minutes(light_seconds),
            sleep_rem_minutes=seconds_to_minutes(rem_seconds),
            sleep_awake_minutes=awake_minutes,
            is_nap=normalized_sleep.get("is_nap", False),
            sleep_stages=normalized_sleep.get("stage_timestamps") or None,
        )

        return event_record_service.create_or_merge_sleep(
            db, user_id, record, detail, settings.sleep_end_gap_minutes
        ).id

    @staticmethod
    def _sleep_score(
        items_by_type: dict[str, Any],
        normalized_sleep: dict[str, Any],
        user_id: UUID,
    ) -> HealthScoreCreate | None:
        """The night's score at its end, with Ultrahuman's contributors and the night's readings as components.

        Ultrahuman publishes no schema for these lists, so an item of an unexpected shape is skipped."""
        raw_sleep = items_by_type["Sleep"]
        quick_metrics = {m.get("type"): m.get("value") for m in _dicts(raw_sleep.get("quick_metrics"))}
        sleep_cycles = raw_sleep.get("sleep_cycles")
        cycles = sleep_cycles.get("cycles") if isinstance(sleep_cycles, dict) else None
        readings = {
            "avg_heart_rate": quick_metrics.get("avg_hr"),
            "avg_hrv": quick_metrics.get("avg_hrv"),
            "sleep_cycles": len(cycles) if isinstance(cycles, list) else None,
        }
        components = {
            **{
                # The display title is the only name a contributor has
                key: ScoreComponent(
                    value=score if _is_number(score := c.get("score")) else None,
                    qualifier=qualifier if isinstance(qualifier := c.get("state_title"), str) else None,
                )
                for c in _dicts(raw_sleep.get("summary"))
                if isinstance(title := c.get("title"), str)
                and (key := re.sub(r"[^a-z0-9]+", "_", title.lower()).strip("_"))
            },
            **_components(HealthScoreCategory.SLEEP, items_by_type),
            **{name: ScoreComponent(value=value) for name, value in readings.items() if _is_number(value)},
        }
        value = next((v for v in (_at(items_by_type, p) for p in SLEEP_SCORE_LOCATIONS) if _is_number(v)), None)
        if value is None and not components:
            return None
        return HealthScoreCreate(
            id=uuid4(),
            user_id=user_id,
            provider=ProviderName.ULTRAHUMAN,
            category=HealthScoreCategory.SLEEP,
            value=value,
            recorded_at=normalized_sleep["end_time"],
            zone_offset=normalized_sleep.get("zone_offset"),
            components=components or None,
        )

    def _save_sleep_score(
        self,
        db: DbSession,
        user_id: UUID,
        items_by_type: dict[str, Any],
        normalized_sleep: dict[str, Any],
        record_id: UUID,
    ) -> None:
        """The night's score on its stored night; a score that fails validation or that the database rejects is logged
        and the night kept. Any other database error fails the night's sync."""
        try:
            with db.begin_nested():
                if score := self._sleep_score(items_by_type, normalized_sleep, user_id):
                    health_score_service.upsert_event_score(db, score.model_copy(update={"event_record_id": record_id}))
        except (ValueError, IntegrityError, DataError) as e:
            logger.warning(
                "Ultrahuman sleep score not saved",
                provider="ultrahuman",
                user_id=str(user_id),
                record_id=str(record_id),
                exc_info=e,
            )

    # -------------------------------------------------------------------------
    # Recovery Data
    # -------------------------------------------------------------------------

    def normalize_recovery(  # ty:ignore[incompatible-method-override]
        self,
        raw_recovery: dict[str, Any],
        user_id: UUID,
    ) -> list[HealthScoreCreate]:
        """One day's scores from its metrics keyed by Ultrahuman type; a score without a value is not written.

        Each score sits at its metric's day_start_timestamp, the instant the day's single-value samples take, at the
        user's offset that day so it reads as the user's date.
        """
        scores: list[HealthScoreCreate] = []
        for metric_type, category in DAILY_SCORES.items():
            metric = raw_recovery.get(metric_type) or {}
            value, day_start = metric.get("value"), metric.get("day_start_timestamp")
            if value is None or not day_start or (recorded_at := _utc(day_start)) is None:
                continue
            scores.append(
                HealthScoreCreate(
                    id=uuid4(),
                    user_id=user_id,
                    provider=ProviderName.ULTRAHUMAN,
                    category=category,
                    value=value,
                    recorded_at=recorded_at,
                    zone_offset=metric.get("ultrahuman_zone_offset"),
                    components=_components(category, raw_recovery) or None,
                )
            )
        return scores

    # -------------------------------------------------------------------------
    # Activity Samples (HR, HRV, Temperature, Steps, Glucose)
    # -------------------------------------------------------------------------

    def normalize_activity_samples(
        self,
        raw_samples: list[dict[str, Any]],
        user_id: UUID,
    ) -> dict[str, list[dict[str, Any]]]:
        """Each timeline's readings by the series they belong to; zero-step readings are dropped."""
        result: dict[str, list[dict[str, Any]]] = {}
        for sample in raw_samples:
            series_type = ACTIVITY_SAMPLE_SERIES.get(sample.get("type", ""))
            if series_type is None:
                continue
            readings = result.setdefault(series_type.value, [])
            for val in sample.get("values", []):
                ts, value = val.get("timestamp"), val.get("value")
                if sample.get("type") in GLUCOSE_METRICS:
                    value = _in_mg_dl(value, sample.get("unit"))
                if not ts or (series_type is SeriesType.steps and not (value and value > 0)):
                    continue
                if (recorded_at := _utc(ts)) is None:
                    continue
                readings.append(
                    {
                        "recorded_at": recorded_at,
                        "zone_offset": sample.get("zone_offset"),
                        "value": value,
                    }
                )
        return result

    def _build_activity_samples(
        self,
        user_id: UUID,
        normalized_samples: dict[str, list[dict[str, Any]]],
    ) -> list[TimeSeriesSampleCreate]:
        """Build TimeSeriesSampleCreate rows from normalized activity samples (HR, HRV, etc.).

        The rows are persisted in bulk by the caller via
        ``timeseries_service.bulk_create_samples`` (upsert), not written here.
        """
        samples: list[TimeSeriesSampleCreate] = []

        for key, entries in normalized_samples.items():
            series_type = SeriesType(key)
            for sample in entries:
                try:
                    samples.append(
                        TimeSeriesSampleCreate(
                            id=uuid4(),
                            user_id=user_id,
                            provider=self.provider_name,
                            recorded_at=sample["recorded_at"],
                            zone_offset=sample.get("zone_offset"),
                            value=Decimal(str(sample["value"])),
                            series_type=series_type,
                            is_daily_total=daily_total_flag(series_type, is_daily=False),
                        )
                    )
                except Exception as e:
                    logger.warning(
                        "Failed to build activity sample",
                        provider="ultrahuman",
                        task="build_activity_samples",
                        series=key,
                        user_id=str(user_id),
                        recorded_at=sample["recorded_at"],
                        error=str(e),
                    )

        return samples

    # -------------------------------------------------------------------------
    # Combined Load (Main Entry Point)
    # -------------------------------------------------------------------------

    def load_and_save_all(
        self,
        db: DbSession,
        user_id: UUID,
        start_time: datetime | str | None = None,
        end_time: datetime | str | None = None,
        is_first_sync: bool = False,
    ) -> Sync247Result:
        """Load and save all 247 data types by fetching daily metrics.

        Ultrahuman exposes one metrics endpoint per day, so the window is walked
        a day at a time and each data type's counts accumulate across days. How a
        day's failed fetch counts is ``_fetch_items_by_type``'s: a lost grant ends
        the run, an outage fails the day (so a live sync keeps its cursor and
        fetches it again, while it is young enough), a throttle that outlasts the
        client's backoff fails the day and ends the walk, and anything else is a
        day without data. A failure while saving is recorded against the data
        type it belongs to.
        """

        # TODO: Extract default backfill days (30) to an env var / settings constant.
        # Not doing it now - this should be unified across all providers at once,
        # since other providers like Garmin, Oura, Whoop each hardcode their own defaults.

        start_time, end_time = self.resolve_window(start_time, end_time)

        # A lost grant or a 401/403 from _fetch_daily_metrics invalidates every remaining
        # day, so it propagates instead of being recorded against one data type.
        run = self.sync_run(db, user_id, fatal=(ProviderConnectionError, ProviderError))
        run.expect("sleep", "recovery", "activity_samples")
        self._fill_provider_user_id(db, user_id)

        current_date = datetime.combine(start_time.date(), datetime.min.time(), tzinfo=timezone.utc)
        end_date = datetime.combine(end_time.date(), datetime.min.time(), tzinfo=timezone.utc)
        while current_date <= end_date:
            try:
                items_by_type = self._fetch_items_by_type(db, user_id, current_date, run)
            except ProviderRateLimitError:
                # Still throttled after the client's backoff: the remaining days would be too, and the
                # failure recorded for daily_metrics keeps the cursor where it was, so they are fetched next run.
                break
            if items_by_type is None:
                current_date += timedelta(days=1)
                continue

            if "Sleep" in items_by_type:
                with run.step("sleep", accumulate=True, commit=True) as step:
                    normalized_sleep = self.normalize_sleep(items_by_type["Sleep"], user_id)
                    record_id = self.save_sleep_data(db, user_id, normalized_sleep)
                    if record_id:
                        self._save_sleep_score(db, user_id, items_by_type, normalized_sleep, record_id)
                    step.record(1 if record_id else 0)

            with run.step("recovery", accumulate=True, commit=True) as step:
                step.record(health_score_service.bulk_upsert(db, self.normalize_recovery(items_by_type, user_id)))

            with run.step("activity_samples", accumulate=True, commit=True) as step:
                step.record(self._save_daily_activity(db, user_id, items_by_type, current_date.date()))

            current_date += timedelta(days=1)

        run.log_summary()
        return run.result

    def _fill_provider_user_id(self, db: DbSession, user_id: UUID) -> None:
        """Fill in the Ultrahuman account id a connect could not read, so the next sync takes the per-account pull
        lock. Best effort, at most once a day per connection and only with the ``profile`` scope /user_info needs.

        The profile is fetched outside the authenticated request path: a 401 there (the scope withdrawn, say) must not
        refresh the token, count towards the dead-grant clock or revoke a connection the metrics work with. Every
        failure, a connection error included, is logged and the sync goes on.
        """
        connection = self.connection_repo.get_by_user_and_provider(db, user_id, self.provider_name)
        if connection is None or connection.provider_user_id:
            return
        if connection.scope and "profile" not in re.split(r"[\s,]+", connection.scope.strip()):
            return
        if not _profile_attempt_allowed(connection.id):
            return
        try:
            access_token = get_valid_token(
                db, user_id, self.provider_name, self.connection_repo, self.oauth, revoke_on_failure=False
            )
            info = fetch_provider_user_info(self.api_base_url, access_token, str(user_id))
        except Exception as e:
            logger.warning(
                "Ultrahuman user profile not fetched; tried again tomorrow",
                provider="ultrahuman",
                task="fill_provider_user_id",
                user_id=str(user_id),
                error_code=error_code_of(e),
            )
            return
        if not info["user_id"]:
            return
        connection.provider_user_id = info["user_id"]
        if info["username"] and not connection.provider_username:
            connection.provider_username = info["username"]
        db.commit()
        logger.info(
            "Ultrahuman account id filled in on sync",
            provider="ultrahuman",
            task="fill_provider_user_id",
            user_id=str(user_id),
        )

    @staticmethod
    def _day_failure(error: Exception) -> DayFailure:
        """What a day's failed fetch means for the run."""
        if isinstance(error, ProviderConnectionError):
            return DayFailure.FATAL
        if not isinstance(error, ProviderError):
            return DayFailure.SKIP  # our own reading of the answer failed: it would fail the same way every run
        status = error.upstream_status
        if isinstance(error, UltrahumanBodyError):
            return DayFailure.FAIL if status is not None and status >= 500 else DayFailure.SKIP
        if status in (401, 403):
            return DayFailure.FATAL
        if status == 404:
            return DayFailure.EMPTY
        if isinstance(error, ProviderRateLimitError):
            return DayFailure.THROTTLED
        if status is None or status >= 500:
            return DayFailure.FAIL
        return DayFailure.SKIP  # 400/422, a 200 that is not JSON: the same request fails the same way every run

    def _fetch_items_by_type(
        self,
        db: DbSession,
        user_id: UUID,
        day: datetime,
        run: Sync247Run,
    ) -> dict[str, Any] | None:
        """Fetch one day of metrics, keyed by Ultrahuman's type name; None when the fetch failed.

        The fetch feeds every data type, so its outcome is recorded against the endpoint (``daily_metrics``)
        rather than any one of them:

        - a lost grant (connection error, 401/403) is raised: every remaining day would get it too;
        - a 404, in the status or in the body's envelope, is a day without data;
        - an outage (5xx, no response) fails the day, so a live sync keeps its cursor and fetches it again; once
          the day is older than ``MAX_RETRIED_DAY_AGE`` a live sync gives it up and reads it as empty;
        - a throttle that outlasted the client's backoff fails the day and is raised, to end the walk;
        - anything else (a request Ultrahuman refused, an answer that is not JSON or that our code could not
          read) would fail the same way every run: the day is skipped as empty, logged and reported.
        """
        date_str = day.strftime("%Y-%m-%d")
        try:
            return self._items_by_type(self._fetch_daily_metrics(db, user_id, day))
        except Exception as e:
            failure = self._day_failure(e)
            if failure is DayFailure.FATAL:
                raise
            if failure is DayFailure.EMPTY:
                self._log_empty_day(user_id, day, error_code_of(e))
                return {}
            if failure is DayFailure.SKIP:
                logger.warning(
                    "Ultrahuman day skipped: its answer can't be used",
                    provider="ultrahuman",
                    task="fetch_daily_metrics",
                    user_id=str(user_id),
                    date=date_str,
                    error_code=error_code_of(e),
                    exc_info=e,
                    alert=True,
                )
                run.result.add("daily_metrics", 0, skipped=1)
                return {}
            if (
                failure is DayFailure.FAIL
                and not _is_historical_run()
                and day.date() < _utc_today() - MAX_RETRIED_DAY_AGE
            ):
                logger.warning(
                    "Ultrahuman day still failing after the retry window; given up as empty",
                    provider="ultrahuman",
                    task="fetch_daily_metrics",
                    user_id=str(user_id),
                    date=date_str,
                    error_code=error_code_of(e),
                )
                run.result.add("daily_metrics", 0, skipped=1)
                return {}
            # An outage is reported; a throttle or a lost response is not, so a day that keeps failing on every
            # pull does not raise an alert each time.
            outage = isinstance(e, ProviderError) and e.upstream_status is not None and e.upstream_status >= 500
            logger.warning(
                "Ultrahuman day not fetched; retried on the next sync",
                provider="ultrahuman",
                task="fetch_daily_metrics",
                user_id=str(user_id),
                date=date_str,
                error_code=error_code_of(e),
                exc_info=e if outage else None,
                alert=outage,
            )
            run.result.fail("daily_metrics", e)
            if failure is DayFailure.THROTTLED:
                raise
            return None

    @staticmethod
    def _log_empty_day(user_id: UUID, day: datetime, reason: str) -> None:
        logger.info(
            "Ultrahuman has no data for the day",
            provider="ultrahuman",
            task="fetch_daily_metrics",
            user_id=str(user_id),
            date=day.strftime("%Y-%m-%d"),
            reason=reason,
        )

    @staticmethod
    def _items_by_type(metrics_list: list[dict[str, Any]]) -> dict[str, Any]:
        """The day's metric objects by type. A type nothing here reads (``workout``, whose payload Ultrahuman does
        not document) is carried along and ignored, never a reason to fail the day."""
        return {
            item["type"]: item["object"]
            for item in metrics_list
            if isinstance(item.get("type"), str) and item["type"] and item.get("object")
        }

    def _save_daily_activity(
        self,
        db: DbSession,
        user_id: UUID,
        items_by_type: dict[str, Any],
        day: date | None = None,
    ) -> WriteCounts:
        """Persist one day's activity series plus its single-value daily metrics."""
        sample_inputs = [
            {
                "type": t,
                "values": items_by_type[t].get("values", []),
                "zone_offset": items_by_type[t].get("ultrahuman_zone_offset"),
                "unit": items_by_type[t].get("unit"),
            }
            for t in ACTIVITY_SAMPLE_SERIES
            if t in items_by_type
        ]
        samples: list[TimeSeriesSampleCreate] = []
        if sample_inputs:
            samples.extend(
                self._build_activity_samples(user_id, self.normalize_activity_samples(sample_inputs, user_id))
            )

        for key, series_type in DAILY_SCALAR_SERIES.items():
            scalar = items_by_type.get(key) or {}
            value, day_start = scalar.get("value"), scalar.get("day_start_timestamp")
            if value is None or not day_start or (recorded_at := _utc(day_start)) is None:
                continue
            try:
                decimal_value = Decimal(str(value))
            except InvalidOperation:
                continue
            zone_offset = scalar.get("ultrahuman_zone_offset")
            samples.append(
                TimeSeriesSampleCreate(
                    id=uuid4(),
                    user_id=user_id,
                    provider=self.provider_name,
                    recorded_at=recorded_at,
                    zone_offset=zone_offset,
                    value=decimal_value,
                    series_type=series_type,
                    is_daily_total=daily_total_flag(series_type, True),
                    period=CalendarPeriod.local_day(recorded_at, zone_offset=zone_offset),
                )
            )

        if not samples:
            return WriteCounts(0, 0)
        return timeseries_service.bulk_create_samples(db, samples)

    # -------------------------------------------------------------------------
    # Abstract Method Implementations
    # -------------------------------------------------------------------------

    def get_sleep_data(
        self,
        db: DbSession,
        user_id: UUID,
        start_time: datetime,
        end_time: datetime,
    ) -> list[dict[str, Any]]:
        """Fetch sleep data from provider API for a date range.

        Returns:
            list[dict[str, Any]]: List of sleep data objects from API.
        """
        sleep_data = []
        current_date = start_time.date()
        end_date = end_time.date()

        while current_date <= end_date:
            metrics_list = self._fetch_daily_metrics(db, user_id, datetime.combine(current_date, datetime.min.time()))
            date_str = current_date.strftime("%Y-%m-%d")

            for item in metrics_list:
                if item.get("type") == "Sleep" and "object" in item:
                    item["object"]["ultrahuman_date"] = date_str
                    sleep_data.append(item["object"])

            current_date = current_date + timedelta(days=1)

        return sleep_data

    def get_recovery_data(
        self,
        db: DbSession,
        user_id: UUID,
        start_time: datetime,
        end_time: datetime,
    ) -> list[dict[str, Any]]:
        """Fetch each day's metrics keyed by Ultrahuman type, the input normalize_recovery takes.

        Returns:
            list[dict[str, Any]]: One dict of metrics per day.
        """
        days = []
        current_date = start_time.date()
        while current_date <= end_time.date():
            day = datetime.combine(current_date, datetime.min.time())
            days.append(self._items_by_type(self._fetch_daily_metrics(db, user_id, day)))
            current_date += timedelta(days=1)
        return days

    def get_activity_samples(
        self,
        db: DbSession,
        user_id: UUID,
        start_time: datetime,
        end_time: datetime,
    ) -> list[dict[str, Any]]:
        """Fetch activity samples (HR, steps, SpO2) from provider API for a date range.

        Returns:
            list[dict[str, Any]]: List of activity sample objects from API.
        """
        samples = []
        current_date = start_time.date()
        end_date = end_time.date()

        while current_date <= end_date:
            metrics_list = self._fetch_daily_metrics(db, user_id, datetime.combine(current_date, datetime.min.time()))
            date_str = current_date.strftime("%Y-%m-%d")

            for item in metrics_list:
                item_type = item.get("type")
                if item_type in ACTIVITY_SAMPLE_SERIES and "object" in item:
                    item["object"]["ultrahuman_date"] = date_str
                    samples.append({"type": item_type, "object": item["object"]})

            current_date = current_date + timedelta(days=1)

        return samples

    def get_daily_activity_statistics(
        self,
        db: DbSession,
        user_id: UUID,
        start_date: datetime,
        end_date: datetime,
    ) -> list[dict[str, Any]]:
        """Fetch aggregated daily activity statistics.

        Ultrahuman does not provide daily activity statistics endpoint.

        Returns:
            list[dict[str, Any]]: Empty list as this is not available.
        """
        return []

    def normalize_daily_activity(
        self,
        raw_stats: dict[str, Any],
        user_id: UUID,
    ) -> dict[str, Any]:
        """Normalize daily activity statistics to our schema.

        Ultrahuman does not provide daily activity statistics.

        Returns:
            dict[str, Any]: Empty dict as this is not available.
        """
        return {}
