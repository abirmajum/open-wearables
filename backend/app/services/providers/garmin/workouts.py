from collections.abc import Mapping
from datetime import datetime, timedelta
from typing import Any, Iterable
from uuid import UUID, uuid4

from app.constants.entry_source import get_unified_garmin_entry_source
from app.constants.workout_types.garmin import get_unified_workout_type
from app.database import DbSession
from app.schemas.enums import EventCategory
from app.schemas.model_crud.activities import (
    EventRecordCreate,
    EventRecordDetailCreate,
    EventRecordMetrics,
)
from app.schemas.providers.garmin import ActivityJSON as GarminActivityJSON
from app.services.event_record_service import event_record_service
from app.services.providers.garmin.coverage import WORKOUT_ENERGY_SERIES
from app.services.providers.templates.base_workouts import BaseWorkoutsTemplate
from app.utils.conversion import as_int, to_decimal
from app.utils.dates import offset_to_iso
from app.utils.structured_logging import get_logger

logger = get_logger(__name__)

# The activity schema names the first; the push handler read the second before, and no real payload settles it
_ELEVATION_FIELDS = ("totalElevationGainInMeters", "elevationGainInMeters")
_CADENCE_FIELDS = (
    "averageRunCadenceInStepsPerMinute",
    "averageBikingCadenceInRevPerMinute",
    "averageSwimCadenceInStrokesPerMinute",
)


def activity_metrics(summary: Mapping[str, Any]) -> EventRecordMetrics:
    """The workout detail of a Garmin activity summary, as the activities push sends it and the activityDetails push
    nests it under ``summary``."""
    metrics: EventRecordMetrics = {
        # Garmin activity payloads carry only average and max heart rate
        "heart_rate_min": None,
        "heart_rate_max": as_int(summary.get("maxHeartRateInBeatsPerMinute")),
        "heart_rate_avg": to_decimal(summary.get("averageHeartRateInBeatsPerMinute")),
        "steps_count": as_int(summary.get("steps")),
        "energy_burned": to_decimal(summary.get("activeKilocalories")),
        "energy_series": WORKOUT_ENERGY_SERIES,
        "distance": to_decimal(summary.get("distanceInMeters")),
        "average_speed": to_decimal(summary.get("averageSpeedInMetersPerSecond")),
        "max_speed": to_decimal(summary.get("maxSpeedInMetersPerSecond")),
        "total_elevation_gain": to_decimal(
            next((e for f in _ELEVATION_FIELDS if (e := summary.get(f)) is not None), None)
        ),
        "average_cadence": to_decimal(next((c for f in _CADENCE_FIELDS if (c := summary.get(f))), None)),
    }

    entry_source = get_unified_garmin_entry_source(summary.get("manual"), summary.get("isWebUpload"))
    if entry_source is not None:
        metrics["entry_source"] = entry_source

    if name := summary.get("activityName"):
        metrics["label"] = name

    return metrics


class GarminWorkouts(BaseWorkoutsTemplate):
    """Garmin implementation of workouts template."""

    def get_workouts(
        self,
        db: DbSession,
        user_id: UUID,
        start_date: datetime,
        end_date: datetime,
    ) -> list[Any]:
        """Get activities from Garmin API for a single time range.

        Note: Garmin API has a maximum range of ~24 hours per request.
        For longer date ranges, use get_workouts_historical().
        """
        # Garmin API uses seconds for timestamps
        start_ts = int(start_date.timestamp())
        end_ts = int(end_date.timestamp())

        params = {
            "uploadStartTimeInSeconds": start_ts,
            "uploadEndTimeInSeconds": end_ts,
        }

        return self._make_api_request(
            db,
            user_id,
            "/wellness-api/rest/activities",
            params=params,
        )

    def get_workouts_historical(
        self,
        db: DbSession,
        user_id: UUID,
        start_date: datetime,
        end_date: datetime,
        chunk_hours: int = 24,
    ) -> list[Any]:
        """Fetch workouts in 24-hour chunks for extended date ranges.

        Garmin API limits requests to ~24 hours. This method fetches data
        in chunks to support historical backfill of any date range.

        Args:
            db: Database session
            user_id: User ID
            start_date: Start of date range
            end_date: End of date range
            chunk_hours: Size of each chunk in hours (default 24)

        Returns:
            List of all activities from the date range
        """
        all_activities: list[Any] = []
        current_start = start_date

        while current_start < end_date:
            current_end = min(current_start + timedelta(hours=chunk_hours), end_date)

            try:
                activities = self.get_workouts(db, user_id, current_start, current_end)
                if isinstance(activities, list):
                    all_activities.extend(activities)
            except Exception as e:
                # Log error but continue with other chunks
                logger.warning(
                    "Error fetching activities chunk",
                    provider="garmin",
                    task="get_workouts_historical",
                    user_id=str(user_id),
                    chunk_start=current_start.isoformat(),
                    chunk_end=current_end.isoformat(),
                    error=str(e),
                )

            current_start = current_end

        return all_activities

    def get_workouts_from_api(self, db: DbSession, user_id: UUID, **kwargs: Any) -> Any:
        """Get activities from Garmin API with options.

        Supports extended date ranges by automatically chunking requests
        when the range exceeds 24 hours (Garmin API limit).
        """
        summary_start_time = kwargs.get("summary_start_time")
        summary_end_time = kwargs.get("summary_end_time")

        start_ts = self._parse_timestamp(summary_start_time)
        end_ts = self._parse_timestamp(summary_end_time)

        # Default to last 24 hours if no time range provided
        if not start_ts:
            start_ts = int((datetime.now() - timedelta(hours=24)).timestamp())
        if not end_ts:
            end_ts = int(datetime.now().timestamp())

        # Convert to datetime for chunked fetching
        start_dt = datetime.fromtimestamp(start_ts)
        end_dt = datetime.fromtimestamp(end_ts)

        # Check if range exceeds 24 hours - if so, use chunked fetching
        if (end_dt - start_dt) > timedelta(hours=24):
            return self.get_workouts_historical(db, user_id, start_dt, end_dt)

        # Single request for ranges <= 24 hours
        params = {
            "uploadStartTimeInSeconds": start_ts,
            "uploadEndTimeInSeconds": end_ts,
        }

        return self._make_api_request(
            db,
            user_id,
            "/wellness-api/rest/activities",
            params=params,
        )

    def get_workout_detail_from_api(self, db: DbSession, user_id: UUID, workout_id: str, **kwargs: Any) -> Any:
        """Get detailed activity data from Garmin API."""
        return self.get_activity_detail(db, user_id, workout_id)

    def _parse_timestamp(self, value: str | None) -> int | None:
        """Parse timestamp from string (Unix timestamp or ISO 8601 date)."""
        if not value:
            return None
        try:
            return int(value)
        except ValueError:
            pass
        try:
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return int(dt.timestamp())
        except (ValueError, AttributeError):
            return None

    def _extract_dates(self, start_timestamp: int, end_timestamp: int) -> tuple[datetime, datetime]:
        """Extract start and end dates from timestamps."""
        start_date = datetime.fromtimestamp(start_timestamp)
        end_date = datetime.fromtimestamp(end_timestamp)
        return start_date, end_date

    def _build_metrics(self, raw_workout: GarminActivityJSON) -> EventRecordMetrics:
        return activity_metrics(raw_workout.model_dump())

    def _normalize_workout(
        self,
        raw_workout: GarminActivityJSON,
        user_id: UUID,
    ) -> tuple[EventRecordCreate, EventRecordDetailCreate]:
        """Normalize Garmin activity to EventRecordCreate and EventRecordDetailCreate."""
        workout_id = uuid4()

        workout_type = get_unified_workout_type(raw_workout.activityType)

        start_date, end_date = self._extract_dates(
            raw_workout.startTimeInSeconds,
            raw_workout.startTimeInSeconds + raw_workout.durationInSeconds,
        )
        duration_seconds = raw_workout.durationInSeconds

        metrics = self._build_metrics(raw_workout)

        device_name = raw_workout.deviceName or "Garmin"

        zone_offset = offset_to_iso(raw_workout.startTimeOffsetInSeconds)

        record = EventRecordCreate(
            category=EventCategory.WORKOUT,
            type=workout_type.value,
            source_name=device_name,
            device_model=raw_workout.deviceName,
            duration_seconds=duration_seconds,
            start_datetime=start_date,
            end_datetime=end_date,
            zone_offset=zone_offset,
            id=workout_id,
            external_id=str(raw_workout.activityId),  # Convert to str (push sends int)
            source="garmin",
            user_id=user_id,
        )

        detail = EventRecordDetailCreate(
            record_id=workout_id,
            **metrics,
        )

        return record, detail

    def _build_bundles(
        self,
        raw: list[GarminActivityJSON],
        user_id: UUID,
    ) -> Iterable[tuple[EventRecordCreate, EventRecordDetailCreate]]:
        """Build event record payloads for Garmin activities."""
        for raw_workout in raw:
            yield self._normalize_workout(raw_workout, user_id)

    def load_data(
        self,
        db: DbSession,
        user_id: UUID,
        **kwargs: Any,
    ) -> int:
        """No-op: Garmin activity data arrives via webhooks.

        REST/summary endpoints are not used. Historical data is fetched
        via the backfill API which delivers data through webhooks.
        """
        logger.info("Garmin activities arrive via webhooks (no REST fetch)", provider="garmin", user_id=str(user_id))
        return 0

    def get_activity_detail(
        self,
        db: DbSession,
        user_id: UUID,
        activity_id: str,
    ) -> dict:
        """Get detailed activity data from Garmin API."""
        return self._make_api_request(db, user_id, f"/wellness-api/rest/activities/{activity_id}")

    def process_push_activities(
        self,
        db: DbSession,
        activities: list[GarminActivityJSON],
        user_id: UUID,
    ) -> list[UUID]:
        """Process activities received from push notification and save to database.

        Args:
            db: Database session
            activities: List of parsed activity data from push webhook
            user_id: Internal user ID (already mapped from Garmin user ID)

        Returns:
            List of created event record IDs
        """
        created_ids: list[UUID] = []

        for record, detail in self._build_bundles(activities, user_id):
            created_record = event_record_service.create(db, record)
            detail_for_record = detail.model_copy(update={"record_id": created_record.id})
            event_record_service.create_detail(db, detail_for_record)
            created_ids.append(created_record.id)

        return created_ids
