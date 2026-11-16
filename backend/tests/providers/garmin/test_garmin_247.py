"""Tests for Garmin 247 data implementation."""

from datetime import date, datetime, timezone
from decimal import Decimal
from itertools import permutations
from typing import Any
from unittest.mock import MagicMock, patch
from uuid import UUID, uuid4

import pytest
from sqlalchemy import event as sa_event
from sqlalchemy.orm import Session

from app.models import (
    DataSource,
    EventRecord,
    EventRecordAggregate,
    HealthScore,
    SeriesAggregate,
    SeriesSample,
    SleepDetails,
    WorkoutDetails,
)
from app.repositories.data_point_series_repository import WriteCounts
from app.repositories.event_record_detail_repository import EventRecordDetailRepository
from app.repositories.event_record_repository import EventRecordRepository
from app.repositories.user_connection_repository import UserConnectionRepository
from app.schemas.enums import ComputedBy, EventCategory, HealthScoreCategory, ProviderName
from app.schemas.enums.series_types import SeriesType, get_series_type_id
from app.schemas.model_crud.activities import CalendarPeriod, HealthScoreCreate
from app.schemas.providers.garmin import ActivityJSON as GarminActivityJSON
from app.schemas.sync_status import SyncScope, SyncSource
from app.services.health_score_service import health_score_service
from app.services.providers.garmin.data_247 import Garmin247Data
from app.services.providers.garmin.handlers.wellness import process_wellness_items
from app.services.providers.garmin.oauth import GarminOAuth
from app.services.providers.garmin.workouts import GarminWorkouts, activity_workout
from app.utils.context import sync_run_var
from app.utils.conversion import to_decimal
from tests.factories import (
    DataSourceFactory,
    EventRecordFactory,
    UserConnectionFactory,
    UserFactory,
    WorkoutDetailsFactory,
)
from tests.fixtures.fit_builder import make_cycling_fit, make_running_fit

# One day's stressDetails as Garmin sends it after midnight, and again in the morning with more data
_AFTER_MIDNIGHT = {"startTimeInSeconds": 1705276800, "timeOffsetBodyBatteryValues": {"0": 20, "3600": 46}}
_MORNING = {"startTimeInSeconds": 1705276800, "timeOffsetBodyBatteryValues": {"0": 20, "3600": 46, "25200": 78}}
# The same day's daily summary, which carries what the Body Battery charged and drained
_DAILY_BODY_BATTERY = {
    "summaryId": "daily_body_battery",
    "calendarDate": "2024-01-15",
    "startTimeInSeconds": 1705276800,
    "bodyBatteryChargedValue": 61,
    "bodyBatteryDrainedValue": 44,
}
# A scored night, 22:00 to 06:00 UTC, with the in-sleep vitals Garmin sends in its sleep summary
_SCORED_NIGHT = {
    "summaryId": "sleep_scored",
    "startTimeInSeconds": 1705269600,
    "startTimeOffsetInSeconds": 3600,
    "durationInSeconds": 28800,
    "deepSleepDurationInSeconds": 7200,
    "lightSleepDurationInSeconds": 14400,
    "remSleepInSeconds": 5400,
    "awakeDurationInSeconds": 1800,
    "overallSleepScore": {"value": 84, "qualifierKey": "GOOD"},
    "timeOffsetSleepSpo2": {"0": 95, "3600": 93},
    "timeOffsetSleepRespiration": {"0": 14.5, "1800": 13.0},
    "averageHeartRate": 54,
    "lowestHeartRate": 47,
    "respirationAvg": 14.2,
    "avgOxygenSaturation": 95.5,
    "sleepHRV": {"value": 48},
}

# Columns that name the row or when it was written, which differ between two users' copies of a workout
_ROW_KEYS = {"id", "record_id", "data_source_id", "created_at"}


class TestGarmin247Data:
    """Tests for Garmin247Data class."""

    @pytest.fixture
    def garmin_247(self, db: Session) -> Garmin247Data:
        """Create Garmin247Data instance for testing."""
        connection_repo = UserConnectionRepository()
        oauth = GarminOAuth(
            user_repo=MagicMock(),
            connection_repo=connection_repo,
            provider_name="garmin",
            api_base_url="https://apis.garmin.com",
        )
        return Garmin247Data(
            provider_name="garmin",
            api_base_url="https://apis.garmin.com",
            oauth=oauth,
        )

    @pytest.fixture
    def sample_sleep(self) -> dict[str, Any]:
        """Sample Garmin sleep data."""
        return {
            "summaryId": "sleep_123",
            "calendarDate": "2024-01-15",
            "startTimeInSeconds": 1705273200,  # 2024-01-14 22:00:00 UTC
            "durationInSeconds": 28800,  # 8 hours
            "deepSleepDurationInSeconds": 7200,  # 2 hours
            "lightSleepDurationInSeconds": 14400,  # 4 hours
            "remSleepInSeconds": 5400,  # 1.5 hours
            "awakeDurationInSeconds": 1800,  # 30 minutes
            "validation": "DEVICE",
        }

    @pytest.fixture
    def sample_daily(self) -> dict[str, Any]:
        """Sample Garmin daily summary data."""
        return {
            "summaryId": "daily_123",
            "calendarDate": "2024-01-15",
            "startTimeInSeconds": 1705276800,  # 2024-01-15 00:00:00 UTC
            "durationInSeconds": 86400,  # 24 hours
            "steps": 12500,
            "distanceInMeters": 9500.5,
            "activeKilocalories": 650,
            "bmrKilocalories": 1800,
            "floorsClimbed": 12,
            "restingHeartRateInBeatsPerMinute": 55,
            "averageHeartRateInBeatsPerMinute": 72,
            "averageStressLevel": 35,
            "timeOffsetHeartRateSamples": {
                "0": 60,
                "900": 65,
                "1800": 70,
            },
        }

    @pytest.fixture
    def sample_epoch(self) -> dict[str, Any]:
        """Sample Garmin epoch data (15-minute interval)."""
        return {
            "summaryId": "epoch_123",
            "startTimeInSeconds": 1705309200,  # 2024-01-15 09:00:00 UTC
            "durationInSeconds": 900,  # 15 minutes
            "steps": 250,
            "distanceInMeters": 200.5,
            "activeKilocalories": 15,
            "meanHeartRateInBeatsPerMinute": 85,
            "maxHeartRateInBeatsPerMinute": 95,
            "intensity": "ACTIVE",
        }

    @pytest.fixture
    def mixed_slot_epochs(self) -> list[dict[str, Any]]:
        """One slot the user partly walked and partly sat through: one epoch per activity type."""
        slot = {"startTimeInSeconds": 1705309200, "startTimeOffsetInSeconds": 3600, "durationInSeconds": 900}
        return [
            {
                **slot,
                "summaryId": "x1a2b3c-65a4f410-3",
                "activityType": "WALKING",
                "steps": 420,
                "activeKilocalories": 18,
            },
            {
                **slot,
                "summaryId": "x1a2b3c-65a4f410-0",
                "activityType": "SEDENTARY",
                "steps": 0,
                "activeKilocalories": 0,
            },
        ]

    @pytest.fixture
    def sample_body_comp(self) -> dict[str, Any]:
        """Sample Garmin body composition data."""
        return {
            "summaryId": "bodycomp_123",
            "measurementTimeInSeconds": 1705320000,  # 2024-01-15 12:00:00 UTC
            "weightInGrams": 75000,  # 75 kg
            "bodyFatInPercent": 18.5,
            "bodyMassIndex": 23.5,
            "muscleMassInGrams": 35000,
            "boneMassInGrams": 3100,
            "bodyWaterInPercent": 56.4,
        }

    @pytest.fixture
    def sample_blood_pressure(self) -> dict[str, Any]:
        """Sample Garmin blood pressure webhook item."""
        return {
            "summaryId": "sd60a3665-6a31b950",
            "systolic": 112,
            "diastolic": 73,
            "pulse": 69,
            "sourceType": "MANUAL",
            "measurementTimeInSeconds": 1781643600,
            "measurementTimeOffsetInSeconds": -18000,  # -05:00
        }

    # -------------------------------------------------------------------------
    # Helper Method Tests
    # -------------------------------------------------------------------------

    def test_epoch_seconds_conversion(self, garmin_247: Garmin247Data) -> None:
        """Test datetime to Unix timestamp conversion."""
        dt = datetime(2024, 1, 15, 12, 0, 0, tzinfo=timezone.utc)
        result = garmin_247._epoch_seconds(dt)
        assert result == 1705320000

    def test_from_epoch_seconds_conversion(self, garmin_247: Garmin247Data) -> None:
        """Test Unix timestamp to datetime conversion."""
        result = garmin_247._from_epoch_seconds(1705320000)
        assert isinstance(result, datetime)
        assert result.year == 2024
        assert result.month == 1
        assert result.day == 15
        assert result.hour == 12
        assert result.tzinfo == timezone.utc

    def test_fetch_in_chunks_single_chunk(self, garmin_247: Garmin247Data, db: Session) -> None:
        """Test chunked fetching for date range under 24 hours."""
        user = UserFactory()
        UserConnectionFactory(user=user, provider="garmin")

        start = datetime(2024, 1, 15, 0, 0, 0, tzinfo=timezone.utc)
        end = datetime(2024, 1, 15, 12, 0, 0, tzinfo=timezone.utc)  # 12 hours

        with patch.object(garmin_247, "_make_api_request", return_value=[{"id": "1"}]) as mock_request:
            result = garmin_247._fetch_in_chunks(db, user.id, "/test", start, end)

            # Should make only 1 request for 12-hour range
            assert mock_request.call_count == 1
            assert len(result) == 1

    def test_fetch_in_chunks_multiple_chunks(self, garmin_247: Garmin247Data, db: Session) -> None:
        """Test chunked fetching for date range over 24 hours."""
        user = UserFactory()
        UserConnectionFactory(user=user, provider="garmin")

        start = datetime(2024, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
        end = datetime(2024, 1, 3, 0, 0, 0, tzinfo=timezone.utc)  # 2 days

        with patch.object(garmin_247, "_make_api_request", return_value=[{"id": "1"}]) as mock_request:
            result = garmin_247._fetch_in_chunks(db, user.id, "/test", start, end)

            # Should make 2 requests for 48-hour range (24h chunks)
            assert mock_request.call_count == 2
            assert len(result) == 2

    def test_fetch_in_chunks_handles_errors(self, garmin_247: Garmin247Data, db: Session) -> None:
        """Test chunked fetching continues on error."""
        user = UserFactory()

        start = datetime(2024, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
        end = datetime(2024, 1, 3, 0, 0, 0, tzinfo=timezone.utc)

        # First call raises error, second succeeds
        with patch.object(
            garmin_247,
            "_make_api_request",
            side_effect=[Exception("API Error"), [{"id": "2"}]],
        ) as mock_request:
            result = garmin_247._fetch_in_chunks(db, user.id, "/test", start, end)

            # Should still return data from successful request
            assert mock_request.call_count == 2
            assert len(result) == 1

    # -------------------------------------------------------------------------
    # Sleep Data Tests
    # -------------------------------------------------------------------------

    def test_normalize_sleep(self, garmin_247: Garmin247Data, sample_sleep: dict[str, Any]) -> None:
        """Test normalizing sleep data."""
        user_id = uuid4()
        normalized, _ = garmin_247.normalize_sleep(sample_sleep, user_id)

        assert normalized["user_id"] == user_id
        assert normalized["provider"] == "garmin"
        assert normalized["duration_seconds"] == 28800
        assert normalized["garmin_summary_id"] == "sleep_123"

        # Sleep stages
        stages = normalized["stages"]
        assert stages["deep_seconds"] == 7200
        assert stages["light_seconds"] == 14400
        assert stages["rem_seconds"] == 5400
        assert stages["awake_seconds"] == 1800

    def test_extract_sleep_stages_from_map(self, garmin_247: Garmin247Data) -> None:
        """Sleep stage intervals are parsed, sorted, and typed correctly."""
        sleep_map = {
            "deep": [{"startTimeInSeconds": 1705276800, "endTimeInSeconds": 1705280400}],
            "light": [{"startTimeInSeconds": 1705273200, "endTimeInSeconds": 1705276800}],
            "rem": [{"startTimeInSeconds": 1705280400, "endTimeInSeconds": 1705284000}],
            "awake": [{"startTimeInSeconds": 1705284000, "endTimeInSeconds": 1705285800}],
            "unknown_stage": [{"startTimeInSeconds": 1705285800, "endTimeInSeconds": 1705287600}],  # skipped
            "deep_bad": [{"startTimeInSeconds": "bad"}],  # skipped
        }
        stages = garmin_247._extract_sleep_stages_from_map(sleep_map)

        assert len(stages) == 4
        assert [s.stage.value for s in stages] == ["light", "deep", "rem", "awake"]
        assert all(s.start_time.tzinfo == timezone.utc for s in stages)

    def test_normalize_sleep_end_datetime_from_stages(self, garmin_247: Garmin247Data) -> None:
        """end_datetime and duration include awake time when sleepLevelsMap is present."""
        user_id = uuid4()
        # start at 22:00, durationInSeconds covers only asleep time (2h)
        start_ts = 1705273200  # 2024-01-14 22:00:00 UTC
        asleep_duration = 7200  # 2 hours (deep + light + rem only)
        awake_end_ts = start_ts + 7200 + 1800  # last awake stage ends 30 min after asleep

        sleep_data: dict[str, Any] = {
            "summaryId": "sleep_stages_test",
            "startTimeInSeconds": start_ts,
            "durationInSeconds": asleep_duration,
            "deepSleepDurationInSeconds": 3600,
            "lightSleepDurationInSeconds": 1800,
            "remSleepInSeconds": 1800,
            "awakeDurationInSeconds": 1800,
            "sleepLevelsMap": {
                "light": [{"startTimeInSeconds": start_ts, "endTimeInSeconds": start_ts + 1800}],
                "deep": [{"startTimeInSeconds": start_ts + 1800, "endTimeInSeconds": start_ts + 5400}],
                "rem": [{"startTimeInSeconds": start_ts + 5400, "endTimeInSeconds": start_ts + 7200}],
                "awake": [{"startTimeInSeconds": start_ts + 7200, "endTimeInSeconds": awake_end_ts}],
            },
        }

        normalized, _ = garmin_247.normalize_sleep(sleep_data, user_id)

        actual_end = datetime.fromisoformat(normalized["end_time"])
        expected_end = datetime.fromtimestamp(awake_end_ts, tz=timezone.utc)
        assert actual_end == expected_end
        assert normalized["duration_seconds"] == 9000  # 7200 asleep + 1800 awake

    def test_sleep_score_is_recorded_at_the_wake_up(
        self, garmin_247: Garmin247Data, sample_sleep: dict[str, Any]
    ) -> None:
        # A night that starts before midnight UTC belongs to the day the user wakes up
        sleep = {**sample_sleep, "startTimeOffsetInSeconds": 3600, "overallSleepScore": {"value": 84}}

        _, score = garmin_247.normalize_sleep(sleep, uuid4())

        assert score is not None
        assert score.recorded_at == datetime(2024, 1, 15, 7, 0, tzinfo=timezone.utc)  # 23:00 UTC + 8 h
        assert score.zone_offset == "+01:00"

    def test_normalize_sleep_missing_stages(self, garmin_247: Garmin247Data) -> None:
        """A manually entered sleep carries no stage fields, so the stages stay unreported."""
        user_id = uuid4()
        sleep_data = {
            "summaryId": "sleep_123",
            "startTimeInSeconds": 1705273200,
            "durationInSeconds": 28800,
            "validation": "MANUAL",
        }

        normalized, _ = garmin_247.normalize_sleep(sleep_data, user_id)
        result = garmin_247._build_sleep_record(user_id, normalized)
        assert result is not None
        _, detail = result

        assert normalized["stages"] == {
            "deep_seconds": None,
            "light_seconds": None,
            "rem_seconds": None,
            "awake_seconds": None,
        }
        assert detail.sleep_deep_minutes is None
        assert detail.sleep_light_minutes is None
        assert detail.sleep_rem_minutes is None
        assert detail.sleep_awake_minutes is None
        assert detail.sleep_total_duration_minutes is None
        assert detail.sleep_efficiency_score is None
        assert detail.sleep_time_in_bed_minutes == 480

    def test_build_sleep_record_keeps_reported_zero_stages(self, garmin_247: Garmin247Data) -> None:
        """Devices without REM tracking report remSleepInSeconds=0, which is a real measurement."""
        user_id = uuid4()
        sleep_data = {
            "summaryId": "sleep_123",
            "startTimeInSeconds": 1705273200,
            "durationInSeconds": 28800,
            "validation": "AUTO_FINAL",
            "deepSleepDurationInSeconds": 7200,
            "lightSleepDurationInSeconds": 21600,
            "remSleepInSeconds": 0,
            "awakeDurationInSeconds": 0,
        }

        normalized, _ = garmin_247.normalize_sleep(sleep_data, user_id)
        result = garmin_247._build_sleep_record(user_id, normalized)
        assert result is not None
        _, detail = result

        assert detail.sleep_rem_minutes == 0
        assert detail.sleep_awake_minutes == 0
        assert detail.sleep_deep_minutes == 120
        assert detail.sleep_total_duration_minutes == 480

    def test_build_sleep_record_keeps_the_validation(self, garmin_247: Garmin247Data) -> None:
        """A *_TENTATIVE night may still grow; the state is kept for is_final."""
        sleep_data = {
            "summaryId": "sleep_123",
            "startTimeInSeconds": 1705273200,
            "durationInSeconds": 28800,
            "validation": "ENHANCED_TENTATIVE",
        }

        normalized, _ = garmin_247.normalize_sleep(sleep_data, uuid4())
        result = garmin_247._build_sleep_record(uuid4(), normalized)

        assert result is not None
        assert result[1].provider_state == "ENHANCED_TENTATIVE"

    def test_a_nap_keeps_its_sleep_summary_validation(self, garmin_247: Garmin247Data) -> None:
        nap = {"napStartTimeInSeconds": 1705320000, "napDurationInSeconds": 1800, "napOffsetInSeconds": 3600}

        result = garmin_247._build_nap_record(nap, uuid4(), "sleep_123", "ENHANCED_FINAL")

        assert result is not None
        assert result[1].provider_state == "ENHANCED_FINAL"

    # -------------------------------------------------------------------------
    # Dailies Data Tests
    # -------------------------------------------------------------------------

    def test_normalize_dailies(self, garmin_247: Garmin247Data, sample_daily: dict[str, Any]) -> None:
        """Test normalizing daily summary data."""
        user_id = uuid4()
        normalized, _ = garmin_247.normalize_dailies(sample_daily, user_id)

        assert normalized["user_id"] == user_id
        assert normalized["calendar_date"] == "2024-01-15"
        assert normalized["steps"] == 12500
        assert normalized["distance_meters"] == 9500.5
        assert normalized["active_calories"] == 650
        assert normalized["resting_heart_rate"] == 55
        assert normalized["floors_climbed"] == 12
        assert normalized["avg_stress"] == 35

        # Heart rate samples
        assert "heart_rate_samples" in normalized
        assert normalized["heart_rate_samples"]["0"] == 60

    def test_normalize_dailies_missing_values(self, garmin_247: Garmin247Data) -> None:
        """Test normalizing daily data with missing values."""
        user_id = uuid4()
        daily_data = {
            "summaryId": "daily_123",
            "calendarDate": "2024-01-15",
            "startTimeInSeconds": 1705276800,
            "durationInSeconds": 86400,
        }

        normalized, _ = garmin_247.normalize_dailies(daily_data, user_id)

        assert normalized["steps"] is None
        assert normalized["active_calories"] is None
        assert normalized["resting_heart_rate"] is None
        assert normalized["exercise_time"] is None

    def test_normalize_dailies_exercise_time(self, garmin_247: Garmin247Data) -> None:
        """Moderate and vigorous seconds are summed into whole exercise minutes."""
        daily_data = {
            "summaryId": "daily_123",
            "calendarDate": "2024-01-15",
            "startTimeInSeconds": 1705276800,
            "moderateIntensityDurationInSeconds": 750,
            "vigorousIntensityDurationInSeconds": 1710,
        }

        normalized, _ = garmin_247.normalize_dailies(daily_data, uuid4())

        assert normalized["exercise_time"] == 41

    def test_normalize_dailies_exercise_time_from_one_field(self, garmin_247: Garmin247Data) -> None:
        """One intensity field alone still gives exercise minutes."""
        daily_data = {
            "summaryId": "daily_123",
            "calendarDate": "2024-01-15",
            "startTimeInSeconds": 1705276800,
            "vigorousIntensityDurationInSeconds": 1680,
        }

        normalized, _ = garmin_247.normalize_dailies(daily_data, uuid4())

        assert normalized["exercise_time"] == 28

    # -------------------------------------------------------------------------
    # Epochs Data Tests
    # -------------------------------------------------------------------------

    def test_normalize_epochs(self, garmin_247: Garmin247Data, sample_epoch: dict[str, Any]) -> None:
        """Test normalizing epoch data."""
        user_id = uuid4()
        epochs = [sample_epoch]

        normalized = garmin_247.normalize_epochs(epochs, user_id)

        assert "heart_rate" in normalized
        assert "steps" in normalized
        assert "energy" in normalized

        assert len(normalized["heart_rate"]) == 1
        assert normalized["heart_rate"][0]["value"] == 85

        assert len(normalized["steps"]) == 1
        assert normalized["steps"][0]["value"] == 250

    def test_normalize_epochs_multiple(self, garmin_247: Garmin247Data) -> None:
        """Test normalizing multiple epochs."""
        user_id = uuid4()
        epochs = [
            {
                "summaryId": "epoch_1",
                "startTimeInSeconds": 1705309200,
                "durationInSeconds": 900,
                "steps": 100,
                "meanHeartRateInBeatsPerMinute": 70,
            },
            {
                "summaryId": "epoch_2",
                "startTimeInSeconds": 1705310100,  # 15 minutes later
                "durationInSeconds": 900,
                "steps": 150,
                "meanHeartRateInBeatsPerMinute": 75,
            },
        ]

        normalized = garmin_247.normalize_epochs(epochs, user_id)

        assert len(normalized["heart_rate"]) == 2
        assert len(normalized["steps"]) == 2

    def test_normalize_epochs_sums_the_activity_types_of_a_slot(
        self, garmin_247: Garmin247Data, mixed_slot_epochs: list[dict[str, Any]]
    ) -> None:
        """A slot's steps and active energy are the sum over its activity types."""
        normalized = garmin_247.normalize_epochs(mixed_slot_epochs, uuid4())

        assert [sample["value"] for sample in normalized["steps"]] == [420]
        assert [sample["value"] for sample in normalized["energy"]] == [18]

    def test_normalize_epochs_counts_a_repeated_epoch_once(
        self, garmin_247: Garmin247Data, mixed_slot_epochs: list[dict[str, Any]]
    ) -> None:
        """A push can carry the same slot and activity type twice; the later one replaces the earlier."""
        repeated = [*mixed_slot_epochs, {**mixed_slot_epochs[0], "steps": 450, "activeKilocalories": 19}]

        normalized = garmin_247.normalize_epochs(repeated, uuid4())

        assert [sample["value"] for sample in normalized["steps"]] == [450]
        assert [sample["value"] for sample in normalized["energy"]] == [19]

    def test_normalize_epochs_sums_a_slot_split_across_requests_of_a_push(
        self, db: Session, garmin_247: Garmin247Data, mixed_slot_epochs: list[dict[str, Any]]
    ) -> None:
        """Garmin splits a push into requests: the second one adds the slot's other activity type to the first."""
        user_id = uuid4()
        walking, sedentary = mixed_slot_epochs

        garmin_247.normalize_epochs([walking], user_id, db)
        normalized = garmin_247.normalize_epochs([sedentary], user_id, db)

        assert [sample["value"] for sample in normalized["steps"]] == [420]
        assert [sample["value"] for sample in normalized["energy"]] == [18]

    def test_requests_of_a_push_lock_their_slots_in_one_order(
        self, db: Session, garmin_247: Garmin247Data, mixed_slot_epochs: list[dict[str, Any]]
    ) -> None:
        """Held to the commit, so a request writes its sum only after the earlier one committed its own."""
        later = {**mixed_slot_epochs[0], "startTimeInSeconds": mixed_slot_epochs[0]["startTimeInSeconds"] + 900}
        locked: list[str] = []

        def capture(*args: Any) -> None:
            if "pg_advisory_xact_lock" in args[2]:
                locked.append(args[3]["key"])

        sa_event.listen(db.get_bind(), "before_cursor_execute", capture)
        try:
            garmin_247.normalize_epochs([later, *mixed_slot_epochs], uuid4(), db)
        finally:
            sa_event.remove(db.get_bind(), "before_cursor_execute", capture)

        assert len(locked) == 2
        assert locked == sorted(locked)

    def test_pulled_epochs_are_not_joined_with_pushed_ones(
        self, db: Session, garmin_247: Garmin247Data, mixed_slot_epochs: list[dict[str, Any]]
    ) -> None:
        """A pull returns every type of its range at once, so it replaces the slot like a later push does."""
        user_id = uuid4()
        walking, sedentary = mixed_slot_epochs
        garmin_247.normalize_epochs([walking], user_id, db)

        normalized = garmin_247.normalize_epochs([sedentary], user_id)

        assert [sample["value"] for sample in normalized["steps"]] == [to_decimal(sedentary["steps"])]

    def test_normalize_epochs_takes_the_slot_window_from_the_latest_epoch(
        self, garmin_247: Garmin247Data, mixed_slot_epochs: list[dict[str, Any]]
    ) -> None:
        """A repeated slot with another duration carries the window of the epoch whose value is stored."""
        repeated = [*mixed_slot_epochs, {**mixed_slot_epochs[0], "durationInSeconds": 600, "steps": 450}]

        normalized = garmin_247.normalize_epochs(repeated, uuid4())

        assert [(sample["value"], sample["window_seconds"]) for sample in normalized["steps"]] == [(450, 600)]

    @pytest.mark.parametrize(
        "malformed",
        [{"startTimeInSeconds": "not-a-timestamp"}, {"startTimeOffsetInSeconds": "n/a"}, {"summaryId": 12345}],
    )
    def test_normalize_epochs_skips_only_a_malformed_epoch(
        self, garmin_247: Garmin247Data, mixed_slot_epochs: list[dict[str, Any]], malformed: dict[str, Any]
    ) -> None:
        """A malformed epoch is dropped on its own; the push's other epochs are kept."""
        broken = {**mixed_slot_epochs[0], "activityType": "RUNNING", "steps": 300, **malformed}

        normalized = garmin_247.normalize_epochs([*mixed_slot_epochs, broken], uuid4())

        assert [sample["value"] for sample in normalized["steps"]] == [420]

    @patch("app.services.providers.garmin.data_247.logger")
    def test_normalize_epochs_reports_skipped_epochs_once(
        self, mock_logger: MagicMock, garmin_247: Garmin247Data, mixed_slot_epochs: list[dict[str, Any]]
    ) -> None:
        """Malformed epochs are reported once per call, with how many were skipped."""
        broken = [
            {**mixed_slot_epochs[0], "summaryId": f"bad-{i}", "startTimeOffsetInSeconds": "n/a"} for i in range(3)
        ]

        garmin_247.normalize_epochs([*mixed_slot_epochs, *broken], uuid4())

        mock_logger.error.assert_called_once()
        assert mock_logger.error.call_args.kwargs["skipped"] == 3

    def test_normalize_epochs_skips_a_non_numeric_metric(
        self, garmin_247: Garmin247Data, mixed_slot_epochs: list[dict[str, Any]]
    ) -> None:
        """A non-numeric metric is left out of the slot's sum; the slot's other epochs still count."""
        running = {**mixed_slot_epochs[0], "activityType": "RUNNING", "steps": "n/a", "activeKilocalories": 9}

        normalized = garmin_247.normalize_epochs([*mixed_slot_epochs, running], uuid4())

        assert [sample["value"] for sample in normalized["steps"]] == [420]
        assert [sample["value"] for sample in normalized["energy"]] == [27]

    # -------------------------------------------------------------------------
    # Body Composition Tests
    # -------------------------------------------------------------------------

    # -------------------------------------------------------------------------
    # Abstract Method Implementation Tests
    # -------------------------------------------------------------------------

    def test_get_recovery_data_returns_empty(self, garmin_247: Garmin247Data, db: Session) -> None:
        """Test that get_recovery_data returns empty list (Garmin doesn't have recovery endpoint)."""
        user_id = uuid4()
        start = datetime(2024, 1, 15, tzinfo=timezone.utc)
        end = datetime(2024, 1, 16, tzinfo=timezone.utc)

        result = garmin_247.get_recovery_data(db, user_id, start, end)

        assert result == []

    def test_normalize_recovery_returns_empty(self, garmin_247: Garmin247Data) -> None:
        """Test that normalize_recovery returns empty dict."""
        result = garmin_247.normalize_recovery({}, uuid4())
        assert result == {}

    # -------------------------------------------------------------------------
    # Integration Tests (with mocks)
    # -------------------------------------------------------------------------

    def test_load_and_save_all_is_noop(self, garmin_247: Garmin247Data, db: Session) -> None:
        """Test load_and_save_all is a no-op (data arrives via webhooks)."""
        user = UserFactory()

        results = garmin_247.load_and_save_all(db, user.id)

        assert results.rows_written == 0
        assert not results.outcomes  # nothing attempted — data arrives by push

    def test_load_and_save_all_with_dates_is_noop(self, garmin_247: Garmin247Data, db: Session) -> None:
        """Test load_and_save_all with custom dates is still a no-op."""
        user = UserFactory()

        start = datetime(2024, 1, 1, tzinfo=timezone.utc)
        end = datetime(2024, 1, 7, tzinfo=timezone.utc)

        results = garmin_247.load_and_save_all(db, user.id, start_time=start, end_time=end)

        assert results.rows_written == 0
        assert not results.outcomes

    def test_load_and_save_all_string_dates_is_noop(self, garmin_247: Garmin247Data, db: Session) -> None:
        """Test load_and_save_all with ISO string dates is still a no-op."""
        user = UserFactory()

        results = garmin_247.load_and_save_all(
            db,
            user.id,
            start_time="2024-01-01T00:00:00Z",
            end_time="2024-01-07T00:00:00Z",
        )

        assert results.rows_written == 0
        assert not results.all_failed

    # -------------------------------------------------------------------------
    # HRV Data Tests
    # -------------------------------------------------------------------------

    def test_process_items_batch_hrv(self, garmin_247: Garmin247Data, db: Session) -> None:
        """Test saving HRV data."""
        user = UserFactory()
        UserConnectionFactory(user=user, provider="garmin")

        hrv_data = {
            "userId": "garmin_user_123",
            "summaryId": "hrv-123",
            "calendarDate": "2026-01-14",
            "lastNightAvg": 84,
            "lastNight5MinHigh": 124,
            "startTimeOffsetInSeconds": 3600,
            "durationInSeconds": 36565,
            "startTimeInSeconds": 1768340715,
            "hrvValues": {
                "265": 70,
                "565": 73,
                "865": 68,
            },
        }

        count = garmin_247.process_items_batch(db, user.id, "hrv", [hrv_data])

        # 3 hrvValues samples; lastNightAvg is the night's day value, kept in series_aggregate.
        assert count == 3
        assert [row.avg for row in db.query(SeriesAggregate)] == [Decimal("84")]

    def test_activity_file_callback_off_garmin_is_never_fetched(self, garmin_247: Garmin247Data, db: Session) -> None:
        """A forged callbackURL must not receive the user's Garmin token."""
        user = UserFactory()
        UserConnectionFactory(user=user, provider="garmin")
        item = {
            "userId": "garmin_user_123",
            "activityId": 123,
            "fileType": "FIT",
            "callbackURL": "https://evil.example/x",
        }

        with patch("app.services.providers.api_client.httpx.get") as mock_get:
            count = garmin_247.process_items_batch(db, user.id, "activityFiles", [item])

        assert count == 0
        mock_get.assert_not_called()

    def test_a_workouts_fit_file_adds_its_training_effect_and_load(
        self, garmin_247: Garmin247Data, db: Session
    ) -> None:
        user = UserFactory()
        UserConnectionFactory(user=user, provider="garmin")
        run = {"activityId": 123, "activityType": "RUNNING", "startTimeInSeconds": 1748764800, "durationInSeconds": 20}
        garmin_247.process_items_batch(db, user.id, "activityDetails", [{"activityId": 123, "summary": run}])
        item = {"activityId": 123, "fileType": "FIT", "callbackURL": "https://apis.garmin.com/file?id=123"}

        with (
            patch("app.services.providers.garmin.data_247.download_binary_content", return_value=make_running_fit()),
            patch("app.services.providers.garmin.data_247.store_fit_file"),
        ):
            garmin_247.process_items_batch(db, user.id, "activityFiles", [item])

        db.expire_all()
        detail = (
            db.query(WorkoutDetails)
            .join(EventRecord, EventRecord.id == WorkoutDetails.record_id)
            .filter(EventRecord.external_id == "123")
            .one()
        )
        assert (detail.aerobic_training_effect, detail.anaerobic_training_effect, detail.training_load) == (
            Decimal("3.4"),
            Decimal("1.2"),
            Decimal("142"),
        )

    # One workout as Garmin pushes it: each push carries some of its fields
    _RUN = {
        "activityId": 321,
        "activityType": "RUNNING",
        "startTimeInSeconds": 1748764800,
        "durationInSeconds": 20,
        "startTimeOffsetInSeconds": 7200,
        "deviceName": "forerunner965",
    }
    _WORKOUT_PUSHES: dict[str, dict[str, Any]] = {
        "activities": {
            **_RUN,
            "userId": "garmin_user_123",
            "activityName": "Morning Run",
            "maxSpeedInMetersPerSecond": 4.1,
            "manual": False,
        },
        "activityDetails": {
            "activityId": 321,
            "summary": {**_RUN, "steps": 8240, "totalElevationGainInMeters": 84.0},
        },
        "activityFiles": {"activityId": 321, "fileType": "FIT", "callbackURL": "https://apis.garmin.com/file?id=321"},
    }

    def _stored_workout(self, garmin_247: Garmin247Data, db: Session, order: tuple[str, ...]) -> dict[str, Any]:
        """A new user's workout after its pushes arrive in this order, each by the path its webhook takes."""
        user = UserFactory()
        UserConnectionFactory(user=user, provider="garmin")
        workouts = GarminWorkouts(
            workout_repo=EventRecordRepository(EventRecord),
            connection_repo=garmin_247.connection_repo,
            provider_name="garmin",
            api_base_url="https://apis.garmin.com",
            oauth=garmin_247.oauth,
        )
        with (
            patch("app.services.providers.garmin.data_247.download_binary_content", return_value=make_running_fit()),
            patch("app.services.providers.garmin.data_247.store_fit_file"),
        ):
            for summary_type in order:
                push = self._WORKOUT_PUSHES[summary_type]
                if summary_type == "activities":
                    workouts.process_push_activities(db, [GarminActivityJSON(**push)], user.id)
                else:
                    garmin_247.process_items_batch(db, user.id, summary_type, [push])

        db.expire_all()
        [(record, detail, source)] = (
            db.query(EventRecord, WorkoutDetails, DataSource)
            .join(WorkoutDetails, WorkoutDetails.record_id == EventRecord.id)
            .join(DataSource, DataSource.id == EventRecord.data_source_id)
            .filter(DataSource.user_id == user.id)
            .all()
        )
        stats = (
            db.query(EventRecordAggregate)
            .filter(EventRecordAggregate.record_id == record.id)
            .order_by(EventRecordAggregate.series_type_definition_id)
            .all()
        )
        return {
            **{c.key: getattr(record, c.key) for c in EventRecord.__table__.columns if c.key not in _ROW_KEYS},
            **{c.key: getattr(detail, c.key) for c in WorkoutDetails.__table__.columns if c.key not in _ROW_KEYS},
            "device_model": source.device_model,
            "stats": [{c.key: getattr(s, c.key) for c in s.__table__.columns if c.key not in _ROW_KEYS} for s in stats],
        }

    @pytest.mark.parametrize("order", list(permutations(_WORKOUT_PUSHES)), ids="-".join)
    def test_a_workouts_pushes_in_any_order_leave_the_same_workout(
        self, garmin_247: Garmin247Data, db: Session, order: tuple[str, ...]
    ) -> None:
        usual = self._stored_workout(garmin_247, db, ("activities", "activityDetails", "activityFiles"))

        assert self._stored_workout(garmin_247, db, order) == usual
        assert (usual["type"], usual["device_model"], usual["zone_offset"]) == ("running", "forerunner965", "+02:00")
        assert (usual["label"], usual["max_speed"], usual["entry_source"]) == (
            "Morning Run",
            Decimal("4.1"),
            "automatic",
        )
        assert (usual["steps_count"], usual["total_elevation_gain"]) == (8240, Decimal("84"))
        assert (usual["aerobic_training_effect"], usual["training_load"]) == (Decimal("3.4"), Decimal("142"))
        assert usual["segments"]

    def test_an_activity_details_push_is_identified_by_its_own_activity_id(
        self, garmin_247: Garmin247Data, db: Session
    ) -> None:
        user = UserFactory()
        UserConnectionFactory(user=user, provider="garmin")
        summary = {k: v for k, v in self._WORKOUT_PUSHES["activityDetails"]["summary"].items() if k != "activityId"}

        garmin_247.process_items_batch(db, user.id, "activityDetails", [{"activityId": 321, "summary": summary}])

        [(record, detail, _)] = self._user_workouts(db, user.id)
        assert (record.external_id, detail.steps_count) == ("321", 8240)

    def test_a_fit_file_without_a_session_pushed_first_is_not_kept(
        self, garmin_247: Garmin247Data, db: Session
    ) -> None:
        user = UserFactory()
        UserConnectionFactory(user=user, provider="garmin")

        with (
            patch("app.services.providers.garmin.data_247.download_binary_content", return_value=make_cycling_fit()),
            patch("app.services.providers.garmin.data_247.store_fit_file"),
        ):
            garmin_247.process_items_batch(db, user.id, "activityFiles", [self._WORKOUT_PUSHES["activityFiles"]])

        assert db.query(EventRecord).count() == 0

    def _garmin_workouts(self, garmin_247: Garmin247Data) -> GarminWorkouts:
        return GarminWorkouts(
            workout_repo=EventRecordRepository(EventRecord),
            connection_repo=garmin_247.connection_repo,
            provider_name="garmin",
            api_base_url="https://apis.garmin.com",
            oauth=garmin_247.oauth,
        )

    @staticmethod
    def _deviceless_source(user: Any, provider: ProviderName = ProviderName.GARMIN) -> DataSource:
        return DataSourceFactory(
            user=user,
            provider=provider,
            source=provider.value,
            device_model=None,
            device_type=None,
            software_version=None,
        )

    @staticmethod
    def _fit_copy(
        source: DataSource,
        external_id: str = "321",
        start: datetime = datetime(2025, 6, 1, 7, 59, 57, tzinfo=timezone.utc),
    ) -> UUID:
        """A second copy of a workout, under no device, as a FIT file pushed beside its summary stored it before
        workouts were locked: it starts earlier, and holds the FIT's fields and a speed of its own."""
        record = EventRecordFactory(
            data_source=source,
            external_id=external_id,
            category=EventCategory.WORKOUT,
            type="running",
            source_name="Garmin",
            start_datetime=start,
            duration_seconds=23,
        )
        WorkoutDetailsFactory(
            event_record=record,
            steps_count=None,
            max_speed=Decimal("9.9"),
            training_load=Decimal("142"),
            segments=[{"lap": 0}],
        )
        return record.id

    def _user_workouts(self, db: Session, user_id: UUID) -> list[tuple[EventRecord, WorkoutDetails, DataSource]]:
        db.expire_all()
        return (
            db.query(EventRecord, WorkoutDetails, DataSource)
            .join(WorkoutDetails, WorkoutDetails.record_id == EventRecord.id)
            .join(DataSource, DataSource.id == EventRecord.data_source_id)
            .filter(DataSource.user_id == user_id)
            .order_by(EventRecord.external_id, DataSource.provider, EventRecord.start_datetime)
            .all()
        )

    def _stored_twice(self, garmin_247: Garmin247Data, db: Session, user: Any) -> tuple[UUID, UUID]:
        """The user's activity 321 stored by its activities push under the device, and again under no device."""
        self._garmin_workouts(garmin_247).process_push_activities(
            db, [GarminActivityJSON(**self._WORKOUT_PUSHES["activities"])], user.id
        )
        [(device_copy, _, _)] = self._user_workouts(db, user.id)
        return device_copy.id, self._fit_copy(self._deviceless_source(user))

    def test_a_workout_stored_twice_is_found_by_its_devices_copy(self, garmin_247: Garmin247Data, db: Session) -> None:
        user = UserFactory()
        device_copy, _ = self._stored_twice(garmin_247, db, user)

        found = EventRecordRepository(EventRecord).get_by_external_ids(
            db, user.id, ["321"], "garmin", EventCategory.WORKOUT
        )

        assert found["321"].id == device_copy

    @pytest.mark.parametrize("summary_type", ["activityDetails", "activityFiles"])
    def test_a_push_to_a_workout_stored_twice_merges_it_into_its_devices_copy(
        self, garmin_247: Garmin247Data, db: Session, summary_type: str
    ) -> None:
        user = UserFactory()
        device_copy, fit_copy = self._stored_twice(garmin_247, db, user)

        with (
            patch("app.services.providers.garmin.data_247.download_binary_content", return_value=make_running_fit()),
            patch("app.services.providers.garmin.data_247.store_fit_file"),
        ):
            garmin_247.process_items_batch(db, user.id, summary_type, [self._WORKOUT_PUSHES[summary_type]])

        [(record, detail, source)] = self._user_workouts(db, user.id)
        assert (record.id, source.device_model) == (device_copy, "forerunner965")
        assert db.get(EventRecord, fit_copy) is None
        assert (detail.label, detail.entry_source, detail.max_speed) == ("Morning Run", "automatic", Decimal("4.1"))
        assert (detail.training_load, detail.heart_rate_avg) == (Decimal("142"), Decimal("145.5"))
        assert detail.segments
        stats = {
            (s.series_type_definition_id, s.computed_by): s
            for s in db.query(EventRecordAggregate).filter(EventRecordAggregate.record_id == device_copy)
        }
        assert stats[(get_series_type_id(SeriesType.heart_rate), ComputedBy.PROVIDER)].avg == Decimal("145.5")
        assert stats[(get_series_type_id(SeriesType.speed), ComputedBy.PROVIDER)].max == Decimal("4.1")
        if summary_type == "activityDetails":
            assert (detail.steps_count, detail.segments) == (8240, [{"lap": 0}])
        else:
            assert detail.aerobic_training_effect == Decimal("3.4")

    def test_merging_a_workout_stored_twice_touches_no_other_record(
        self, garmin_247: Garmin247Data, db: Session
    ) -> None:
        user, other_user = UserFactory(), UserFactory()
        _, fit_copy = self._stored_twice(garmin_247, db, user)
        other_users_copies = set(self._stored_twice(garmin_247, db, other_user))
        other_activity = self._fit_copy(
            db.query(DataSource).filter(DataSource.user_id == user.id, DataSource.device_model.is_(None)).one(),
            "999",
            datetime(2025, 6, 2, 7, 0, tzinfo=timezone.utc),
        )
        other_provider = self._fit_copy(self._deviceless_source(user, ProviderName.POLAR))

        garmin_247.process_items_batch(db, user.id, "activityDetails", [self._WORKOUT_PUSHES["activityDetails"]])

        assert db.get(EventRecord, fit_copy) is None
        assert {r.id for r, _, _ in self._user_workouts(db, other_user.id)} == other_users_copies
        kept = {(r.id, r.external_id, s.provider, s.device_model) for r, _, s in self._user_workouts(db, user.id)}
        assert {(i, e, p) for i, e, p, d in kept if d is None} == {
            (other_activity, "999", "garmin"),
            (other_provider, "321", "polar"),
        }

    def test_copies_of_a_workout_under_two_devices_are_both_kept(self, garmin_247: Garmin247Data, db: Session) -> None:
        user = UserFactory()
        _, fit_copy = self._stored_twice(garmin_247, db, user)
        second_device = DataSourceFactory(
            user=user, provider=ProviderName.GARMIN, source="garmin", device_model="edge1040"
        )
        db.query(EventRecord).filter(EventRecord.id == fit_copy).update({"data_source_id": second_device.id})

        garmin_247.process_items_batch(db, user.id, "activityDetails", [self._WORKOUT_PUSHES["activityDetails"]])

        assert len(self._user_workouts(db, user.id)) == 2

    def test_process_items_batch_hrv_missing_start_time(self, garmin_247: Garmin247Data, db: Session) -> None:
        """Test saving HRV data with missing start time."""
        user = UserFactory()
        UserConnectionFactory(user=user, provider="garmin")

        hrv_data = {
            "userId": "garmin_user_123",
            "lastNightAvg": 84,
            # Missing startTimeInSeconds
        }

        count = garmin_247.process_items_batch(db, user.id, "hrv", [hrv_data])

        assert count == 0

    def test_process_items_batch_hrv_only_avg(self, garmin_247: Garmin247Data, db: Session) -> None:
        """Test saving HRV data with only lastNightAvg."""
        user = UserFactory()
        UserConnectionFactory(user=user, provider="garmin")

        hrv_data = {
            "userId": "garmin_user_123",
            "summaryId": "hrv-123",
            "calendarDate": "2026-01-14",
            "lastNightAvg": 84,
            "startTimeInSeconds": 1768340715,
            # No hrvValues
        }

        count = garmin_247.process_items_batch(db, user.id, "hrv", [hrv_data])

        # lastNightAvg alone: no sample, one day row.
        assert count == 0
        assert db.query(SeriesAggregate).count() == 1

    # -------------------------------------------------------------------------
    # Build Method Tests (no DB interaction)
    # -------------------------------------------------------------------------

    def test_build_dailies_samples(self, garmin_247: Garmin247Data, sample_daily: dict[str, Any]) -> None:
        """Test _build_dailies_samples returns samples without DB call."""
        user_id = uuid4()
        normalized, _ = garmin_247.normalize_dailies(sample_daily, user_id)
        samples = garmin_247._build_dailies_samples(user_id, normalized)

        # 6 metrics (steps, active/basal calories, resting_hr, floors, distance), the day's average HR + 3 HR samples
        assert len(samples) == 10
        assert len({s.series_type for s in samples}) > 1

    def test_build_dailies_samples_exercise_time(self, garmin_247: Garmin247Data) -> None:
        """Dailies emit exercise_time as a daily-total sample."""
        user_id = uuid4()
        daily_data = {
            "summaryId": "daily_123",
            "calendarDate": "2024-01-15",
            "startTimeInSeconds": 1705276800,
            "moderateIntensityDurationInSeconds": 720,
            "vigorousIntensityDurationInSeconds": 1680,
        }
        normalized, _ = garmin_247.normalize_dailies(daily_data, user_id)

        samples = garmin_247._build_dailies_samples(user_id, normalized)

        exercise = [s for s in samples if s.series_type == SeriesType.exercise_time]
        assert [s.value for s in exercise] == [Decimal("40")]
        assert exercise[0].is_daily_total is True

    def test_build_dailies_samples_empty_on_missing_date(self, garmin_247: Garmin247Data) -> None:
        """Test _build_dailies_samples returns empty for missing date/timestamp."""
        user_id = uuid4()
        normalized = {"user_id": user_id}
        samples = garmin_247._build_dailies_samples(user_id, normalized)
        assert samples == []

    def test_build_epochs_samples(self, garmin_247: Garmin247Data, sample_epoch: dict[str, Any]) -> None:
        """Test _build_epochs_samples returns samples without DB call."""
        user_id = uuid4()
        normalized = garmin_247.normalize_epochs([sample_epoch], user_id)
        samples = garmin_247._build_epochs_samples(user_id, normalized)

        # 1 heart_rate + 1 steps + 1 energy = 3
        assert len(samples) == 3

    def test_build_epochs_samples_carry_the_window(
        self, garmin_247: Garmin247Data, sample_epoch: dict[str, Any]
    ) -> None:
        user_id = uuid4()
        normalized = garmin_247.normalize_epochs([sample_epoch], user_id)

        samples = garmin_247._build_epochs_samples(user_id, normalized)

        assert {s.window_seconds for s in samples} == {900}

    def test_build_epochs_samples_keep_an_epoch_reporting_no_duration(
        self, garmin_247: Garmin247Data, sample_epoch: dict[str, Any]
    ) -> None:
        user_id = uuid4()
        normalized = garmin_247.normalize_epochs([{**sample_epoch, "durationInSeconds": 0}], user_id)

        samples = garmin_247._build_epochs_samples(user_id, normalized)

        assert len(samples) == 3
        assert {s.window_seconds for s in samples} == {None}

    def test_build_dailies_samples_carry_the_whole_garmin_local_day(
        self, garmin_247: Garmin247Data, sample_daily: dict[str, Any]
    ) -> None:
        user_id = uuid4()
        mid_day_sync = {**sample_daily, "startTimeOffsetInSeconds": 3600, "durationInSeconds": 50400}
        normalized, _ = garmin_247.normalize_dailies(mid_day_sync, user_id)

        samples = garmin_247._build_dailies_samples(user_id, normalized)

        day = CalendarPeriod.local_day(datetime(2024, 1, 15, tzinfo=timezone.utc), 86400, "+01:00")
        assert {s.period for s in samples if s.series_type != SeriesType.heart_rate} == {day}
        # Garmin's own day average is a day value; the intraday readings stay samples
        assert {s.period for s in samples if s.series_type == SeriesType.heart_rate} == {
            day.model_copy(update={"stat": "avg"}),
            None,
        }

    def test_build_dailies_samples_without_start_time_keep_the_date(self, garmin_247: Garmin247Data) -> None:
        user_id = uuid4()
        normalized, _ = garmin_247.normalize_dailies({"calendarDate": "2024-01-15", "steps": 100}, user_id)

        samples = garmin_247._build_dailies_samples(user_id, normalized)

        assert [s.period for s in samples] == [CalendarPeriod.of_date(date(2024, 1, 15))]

    def test_process_items_batch_dailies_mirrors_the_day_values(
        self, db: Session, garmin_247: Garmin247Data, sample_daily: dict[str, Any]
    ) -> None:
        user = UserFactory()

        garmin_247.process_items_batch(db, user.id, "dailies", [sample_daily])

        # steps, active and basal energy, resting HR, floors, distance, Garmin's day HR; intraday HR stays a sample
        assert db.query(SeriesAggregate).count() == 7

    def test_dailies_intensity_minutes_are_day_values_of_their_own(
        self, db: Session, garmin_247: Garmin247Data, sample_daily: dict[str, Any]
    ) -> None:
        user = UserFactory()
        daily = {**sample_daily, "moderateIntensityDurationInSeconds": 1800, "vigorousIntensityDurationInSeconds": 930}

        garmin_247.process_items_batch(db, user.id, "dailies", [daily])

        days = {row.series_type_definition_id: row.sum for row in db.query(SeriesAggregate)}
        assert [
            days[get_series_type_id(series_type)]
            for series_type in (
                SeriesType.moderate_intensity_time,
                SeriesType.vigorous_intensity_time,
                SeriesType.exercise_time,
            )
        ] == [30, 15, 45]

    def test_dailies_heart_rate_is_garmins_own_day_value(
        self, db: Session, garmin_247: Garmin247Data, sample_daily: dict[str, Any]
    ) -> None:
        user = UserFactory()
        daily = {**sample_daily, "minHeartRateInBeatsPerMinute": 48, "maxHeartRateInBeatsPerMinute": 161}

        garmin_247.process_items_batch(db, user.id, "dailies", [daily])

        day = (
            db.query(SeriesAggregate)
            .join(DataSource, DataSource.id == SeriesAggregate.data_source_id)
            .filter(
                DataSource.user_id == user.id,
                SeriesAggregate.series_type_definition_id == get_series_type_id(SeriesType.heart_rate),
            )
            .one()
        )
        assert (day.computed_by, day.avg, day.min, day.max) == (ComputedBy.PROVIDER, 72, 48, 161)

    def test_dailies_put_max_stress_and_stress_durations_on_the_stress_score(
        self, db: Session, garmin_247: Garmin247Data, sample_daily: dict[str, Any]
    ) -> None:
        user = UserFactory()
        daily = {
            **sample_daily,
            "startTimeOffsetInSeconds": 3600,
            "maxStressLevel": 91,
            "stressDurationInSeconds": 14400,
            "restStressDurationInSeconds": 28800,
            "activityStressDurationInSeconds": 7200,
            "lowStressDurationInSeconds": 9000,
            "mediumStressDurationInSeconds": 3600,
            "highStressDurationInSeconds": 1800,
        }

        garmin_247.process_items_batch(db, user.id, "dailies", [daily])

        stress = self._scores(db, user.id, HealthScoreCategory.STRESS)
        assert [(s.value, s.zone_offset) for s in stress] == [(Decimal("35"), "+01:00")]
        assert {name: c["value"] for name, c in stress[0].components.items()} == {
            "max_stress_level": 91,
            "stress_duration_seconds": 14400,
            "rest_stress_duration_seconds": 28800,
            "activity_stress_duration_seconds": 7200,
            "low_stress_duration_seconds": 9000,
            "medium_stress_duration_seconds": 3600,
            "high_stress_duration_seconds": 1800,
        }

    def test_a_max_stress_garmin_could_not_measure_is_not_stored(
        self, db: Session, garmin_247: Garmin247Data, sample_daily: dict[str, Any]
    ) -> None:
        user = UserFactory()

        garmin_247.process_items_batch(
            db, user.id, "dailies", [{**sample_daily, "maxStressLevel": -1, "stressDurationInSeconds": 14400}]
        )

        (stress,) = self._scores(db, user.id, HealthScoreCategory.STRESS)
        assert {name: c["value"] for name, c in stress.components.items()} == {"stress_duration_seconds": 14400}

    def test_a_later_dailies_push_refreshes_the_days_stress_score(
        self, db: Session, garmin_247: Garmin247Data, sample_daily: dict[str, Any]
    ) -> None:
        user = UserFactory()
        morning = {**sample_daily, "stressQualifier": "calm", "maxStressLevel": 60}
        evening = {**sample_daily, "averageStressLevel": 42, "stressQualifier": "stressful", "maxStressLevel": 93}

        garmin_247.process_items_batch(db, user.id, "dailies", [morning])
        garmin_247.process_items_batch(db, user.id, "dailies", [evening])

        stress = self._scores(db, user.id, HealthScoreCategory.STRESS)
        assert [(s.value, s.qualifier, s.components["max_stress_level"]["value"]) for s in stress] == [
            (Decimal("42"), "Stressful", 93)
        ]

    def test_one_push_with_a_day_twice_keeps_its_last_stress_score(
        self, db: Session, garmin_247: Garmin247Data, sample_daily: dict[str, Any]
    ) -> None:
        user = UserFactory()

        garmin_247.process_items_batch(
            db, user.id, "dailies", [sample_daily, {**sample_daily, "averageStressLevel": 42}]
        )

        assert [s.value for s in self._scores(db, user.id, HealthScoreCategory.STRESS)] == [Decimal("42")]

    def test_build_body_comp_samples(self, garmin_247: Garmin247Data, sample_body_comp: dict[str, Any]) -> None:
        """Test _build_body_comp_samples returns samples without DB call."""
        user_id = uuid4()
        samples = garmin_247._build_body_comp_samples(user_id, sample_body_comp)
        assert len(samples) == 6  # weight, body_fat, BMI, skeletal muscle mass, bone mass, body water

        by_type = {s.series_type: s for s in samples}
        assert by_type[SeriesType.skeletal_muscle_mass].value == Decimal("35")  # 35000 g -> 35 kg
        assert by_type[SeriesType.bone_mass].value == Decimal("3.1")  # 3100 g -> 3.1 kg
        assert by_type[SeriesType.body_water_percentage].value == Decimal("56.4")

    def test_build_body_comp_samples_missing_timestamp(self, garmin_247: Garmin247Data) -> None:
        """Test _build_body_comp_samples returns empty for missing timestamp."""
        user_id = uuid4()
        samples = garmin_247._build_body_comp_samples(user_id, {"weightInGrams": 75000})
        assert samples == []

    @patch("app.repositories.data_point_series_repository.DataPointSeriesRepository.bulk_create")
    def test_process_items_batch_body_comp(
        self,
        mock_bulk_create: MagicMock,
        garmin_247: Garmin247Data,
        db: Session,
        sample_body_comp: dict[str, Any],
    ) -> None:
        """Test body composition is persisted through the batch entrypoint."""
        user = UserFactory()
        mock_bulk_create.side_effect = lambda db_session, samples: WriteCounts(len(samples), 0)

        count = garmin_247.process_items_batch(db, user.id, "bodyComps", [sample_body_comp])

        # Should create 6 data points: weight, body_fat, BMI, skeletal muscle mass, bone mass, body water
        mock_bulk_create.assert_called_once()
        assert len(mock_bulk_create.call_args[0][1]) == 6
        assert count == 6

    def test_process_items_batch_body_comp_missing_timestamp(self, garmin_247: Garmin247Data, db: Session) -> None:
        """Test a body composition item without a timestamp writes nothing."""
        user = UserFactory()
        body_comp = {"summaryId": "bc_123", "weightInGrams": 75000}

        count = garmin_247.process_items_batch(db, user.id, "bodyComps", [body_comp])

        assert count == 0

    def test_build_blood_pressure_samples(
        self, garmin_247: Garmin247Data, sample_blood_pressure: dict[str, Any]
    ) -> None:
        """Test _build_blood_pressure_samples reads the webhook timestamp field."""
        user_id = uuid4()
        samples = garmin_247._build_blood_pressure_samples(user_id, sample_blood_pressure)

        assert len(samples) == 2  # systolic + diastolic
        by_type = {s.series_type: s for s in samples}
        assert by_type[SeriesType.blood_pressure_systolic].value == Decimal("112")
        assert by_type[SeriesType.blood_pressure_diastolic].value == Decimal("73")

        systolic = by_type[SeriesType.blood_pressure_systolic]
        assert systolic.recorded_at == datetime(2026, 6, 16, 21, 0, tzinfo=timezone.utc)
        assert systolic.zone_offset == "-05:00"
        assert systolic.external_id == "sd60a3665-6a31b950"

    def test_build_blood_pressure_samples_missing_timestamp(self, garmin_247: Garmin247Data) -> None:
        """Test _build_blood_pressure_samples returns empty when timestamp is absent."""
        user_id = uuid4()
        samples = garmin_247._build_blood_pressure_samples(user_id, {"systolic": 112, "diastolic": 73})
        assert samples == []

    def test_build_hrv_samples(self, garmin_247: Garmin247Data) -> None:
        """Test _build_hrv_samples returns samples without DB call."""
        user_id = uuid4()
        hrv_data = {
            "summaryId": "hrv-123",
            "startTimeInSeconds": 1768340715,
            "lastNightAvg": 84,
            "hrvValues": {"265": 70, "565": 73},
        }
        samples = garmin_247._build_hrv_samples(user_id, hrv_data)
        assert len(samples) == 3  # 1 avg + 2 values

    def test_build_stress_samples(self, garmin_247: Garmin247Data) -> None:
        """Test _build_stress_samples returns samples without DB call."""
        user_id = uuid4()
        stress_data = {
            "startTimeInSeconds": 1705276800,
            "timeOffsetStressLevelValues": {"0": 25, "300": 30, "600": -1},  # -1 is skipped
            "timeOffsetBodyBatteryValues": {"0": 80, "300": 78},
        }
        samples = garmin_247._build_stress_samples(user_id, stress_data)
        assert len(samples) == 4  # 2 stress + 2 battery

    # Webhook pushes and backfills both go through process_items_batch
    def test_a_later_push_of_the_day_raises_the_body_battery_peak(self, garmin_247: Garmin247Data, db: Session) -> None:
        user = UserFactory()

        garmin_247.process_items_batch(db, user.id, "stressDetails", [_AFTER_MIDNIGHT])
        garmin_247.process_items_batch(db, user.id, "stressDetails", [_MORNING])

        assert self._body_battery_peaks(db, user.id) == [Decimal("78")]

    def test_an_earlier_push_arriving_last_keeps_the_body_battery_peak(
        self, garmin_247: Garmin247Data, db: Session
    ) -> None:
        user = UserFactory()

        garmin_247.process_items_batch(db, user.id, "stressDetails", [_MORNING])
        garmin_247.process_items_batch(db, user.id, "stressDetails", [_AFTER_MIDNIGHT])

        assert self._body_battery_peaks(db, user.id) == [Decimal("78")]

    @pytest.mark.parametrize(
        "pushes",
        [
            [("stressDetails", _MORNING), ("dailies", _DAILY_BODY_BATTERY)],
            [("dailies", _DAILY_BODY_BATTERY), ("stressDetails", _MORNING)],
        ],
        ids=["peak-first", "dailies-first"],
    )
    def test_dailies_add_charged_and_drained_to_the_days_body_battery(
        self, garmin_247: Garmin247Data, db: Session, pushes: list[tuple[str, dict[str, Any]]]
    ) -> None:
        user = UserFactory()

        for summary_type, item in pushes:
            garmin_247.process_items_batch(db, user.id, summary_type, [item])

        body_battery = self._scores(db, user.id, HealthScoreCategory.BODY_BATTERY)
        assert [(s.value, s.components) for s in body_battery] == [
            (Decimal("78"), {"charged": {"value": 61, "qualifier": None}, "drained": {"value": 44, "qualifier": None}})
        ]

    def test_a_later_daily_keeps_the_body_battery_peak(self, garmin_247: Garmin247Data, db: Session) -> None:
        user = UserFactory()

        garmin_247.process_items_batch(db, user.id, "stressDetails", [_MORNING])
        garmin_247.process_items_batch(db, user.id, "dailies", [_DAILY_BODY_BATTERY])
        garmin_247.process_items_batch(db, user.id, "dailies", [{**_DAILY_BODY_BATTERY, "bodyBatteryChargedValue": 70}])

        body_battery = self._scores(db, user.id, HealthScoreCategory.BODY_BATTERY)
        assert [(s.value, s.components["charged"]["value"]) for s in body_battery] == [(Decimal("78"), 70)]

    @staticmethod
    def _scores(db: Session, user_id: UUID, category: HealthScoreCategory) -> list[HealthScore]:
        db.expire_all()
        return (
            db.query(HealthScore)
            .filter(
                HealthScore.user_id == user_id,
                HealthScore.provider == ProviderName.GARMIN,
                HealthScore.category == category,
            )
            .all()
        )

    @staticmethod
    def _body_battery_peaks(db: Session, user_id: UUID) -> list[Decimal]:
        scores = db.query(HealthScore).filter(
            HealthScore.user_id == user_id, HealthScore.category == HealthScoreCategory.BODY_BATTERY
        )
        return [score.value for score in scores]

    def test_build_respiration_samples_webhook_field(self, garmin_247: Garmin247Data) -> None:
        """allDayRespiration webhook payload uses timeOffsetEpochToBreaths."""
        user_id = uuid4()
        respiration_data = {
            "summaryId": "x36bc70e-6a3c0608",
            "startTimeInSeconds": 1782318600,
            "startTimeOffsetInSeconds": 7200,
            "timeOffsetEpochToBreaths": {"0": 11.33, "60": 15.36, "120": 16.0},
        }
        samples = garmin_247._build_respiration_samples(user_id, respiration_data)

        assert len(samples) == 3
        assert all(s.series_type == SeriesType.respiratory_rate for s in samples)
        first = min(samples, key=lambda s: s.recorded_at)
        assert first.recorded_at == datetime(2026, 6, 24, 16, 30, tzinfo=timezone.utc)
        assert first.value == Decimal("11.33")
        assert first.zone_offset == "+02:00"

    def test_build_sleep_record(self, garmin_247: Garmin247Data, sample_sleep: dict[str, Any]) -> None:
        """Test _build_sleep_record returns record + detail without DB call."""
        user_id = uuid4()
        normalized, _ = garmin_247.normalize_sleep(sample_sleep, user_id)
        result = garmin_247._build_sleep_record(user_id, normalized)

        assert result is not None
        record, detail = result
        assert record.category == "sleep"
        assert record.type == "sleep_session"
        assert detail.sleep_deep_minutes == 120  # 7200 / 60

    def test_build_sleep_detail_orm_object(self, garmin_247: Garmin247Data, sample_sleep: dict[str, Any]) -> None:
        """Regression test for #1135: real Garmin sleep payloads (no heart rate fields -
        Garmin's ClientSleep schema doesn't expose them) must build a SleepDetails ORM
        object without TypeError."""
        user_id = uuid4()
        normalized, _ = garmin_247.normalize_sleep(sample_sleep, user_id)
        result = garmin_247._build_sleep_record(user_id, normalized)
        assert result is not None
        _, detail = result

        repo = EventRecordDetailRepository.__new__(EventRecordDetailRepository)
        orm_detail = repo._build_detail(detail, "sleep")
        assert isinstance(orm_detail, SleepDetails)

    def test_build_activity_record(self, garmin_247: Garmin247Data) -> None:
        """Test activity_workout returns record + detail without DB call."""
        user_id = uuid4()
        activity = {
            "activityId": 123456,
            "startTimeInSeconds": 1705309200,
            "durationInSeconds": 3600,
            "activityType": "RUNNING",
            "distanceInMeters": 5000,
            "activeKilocalories": 350,
        }
        result = activity_workout(activity, user_id)

        assert result is not None
        record, detail = result
        assert record.category == "workout"
        assert record.type == "running"
        assert detail.distance == Decimal("5000")

    @pytest.mark.parametrize("field", ["totalElevationGainInMeters", "elevationGainInMeters"])
    def test_either_elevation_gain_field_fills_the_workout(self, garmin_247: Garmin247Data, field: str) -> None:
        summary = {"activityId": 1, "activityType": "RUNNING", "startTimeInSeconds": 1705309200, field: 84.0}

        result = activity_workout(summary, uuid4())

        assert result is not None
        assert result[1].total_elevation_gain == Decimal("84.0")

    def test_an_activity_details_summary_fills_the_workout_like_the_activities_push(
        self, garmin_247: Garmin247Data
    ) -> None:
        summary = {
            "activityId": 123456,
            "activityName": "Morning Run",
            "activityType": "RUNNING",
            "startTimeInSeconds": 1705309200,
            "durationInSeconds": 2940,
            "steps": 8240,
            "averageSpeedInMetersPerSecond": 3.05,
            "maxSpeedInMetersPerSecond": 4.1,
            "totalElevationGainInMeters": 84.0,
            "averageRunCadenceInStepsPerMinute": 168.0,
            "manual": False,
        }

        result = activity_workout(summary, uuid4())

        assert result is not None
        _, detail = result
        assert (
            detail.total_elevation_gain,
            detail.average_speed,
            detail.max_speed,
            detail.steps_count,
            detail.average_cadence,
            detail.label,
            detail.entry_source,
        ) == (Decimal("84.0"), Decimal("3.05"), Decimal("4.1"), 8240, Decimal("168.0"), "Morning Run", "automatic")

    def test_build_activity_record_missing_id(self, garmin_247: Garmin247Data) -> None:
        """Test activity_workout returns None for missing activityId."""
        user_id = uuid4()
        result = activity_workout({"startTimeInSeconds": 1705309200}, user_id)
        assert result is None

    def test_build_moveiq_record(self, garmin_247: Garmin247Data) -> None:
        """Test _build_moveiq_record returns record without DB call."""
        user_id = uuid4()
        moveiq = {
            "startTimeInSeconds": 1705309200,
            "durationInSeconds": 600,
            "activityType": "WALKING",
            "summaryId": "moveiq_123",
        }
        record = garmin_247._build_moveiq_record(user_id, moveiq)
        assert record is not None
        assert record.type == "moveiq_walking"
        assert record.category == "activity"

    def test_build_moveiq_record_missing_start(self, garmin_247: Garmin247Data) -> None:
        """Test _build_moveiq_record returns None for missing startTime."""
        user_id = uuid4()
        record = garmin_247._build_moveiq_record(user_id, {"activityType": "WALKING"})
        assert record is None

    # -------------------------------------------------------------------------
    # Batch Processing Tests
    # -------------------------------------------------------------------------

    @patch("app.repositories.data_point_series_repository.DataPointSeriesRepository.bulk_create")
    def test_process_items_batch_dailies(
        self,
        mock_bulk_create: MagicMock,
        garmin_247: Garmin247Data,
        db: Session,
        sample_daily: dict[str, Any],
    ) -> None:
        """Test batch processing multiple daily items in a single bulk_create."""
        user = UserFactory()
        daily2 = {**sample_daily, "summaryId": "daily_456", "calendarDate": "2024-01-16"}
        mock_bulk_create.side_effect = lambda db_session, samples: WriteCounts(len(samples), 0)

        count = garmin_247.process_items_batch(db, user.id, "dailies", [sample_daily, daily2])

        # Should call bulk_create exactly once for all samples
        mock_bulk_create.assert_called_once()
        assert count > 0

    def test_process_items_batch_epochs_stores_the_slot_sum(
        self, garmin_247: Garmin247Data, db: Session, mixed_slot_epochs: list[dict[str, Any]]
    ) -> None:
        """The epochs of one push are summed per slot instead of the last activity type overwriting the others."""
        user = UserFactory()

        garmin_247.process_items_batch(db, user.id, "epochs", mixed_slot_epochs)

        stored = (
            db.query(SeriesSample.series_type_definition_id, SeriesSample.value)
            .join(DataSource, DataSource.id == SeriesSample.data_source_id)
            .filter(DataSource.user_id == user.id)
            .all()
        )
        assert sorted((type_id, float(value)) for type_id, value in stored) == sorted(
            [(get_series_type_id(SeriesType.steps), 420.0), (get_series_type_id(SeriesType.active_energy), 18.0)]
        )

    @patch("app.repositories.data_point_series_repository.DataPointSeriesRepository.bulk_create")
    def test_process_items_batch_stress(
        self,
        mock_bulk_create: MagicMock,
        garmin_247: Garmin247Data,
        db: Session,
    ) -> None:
        """Test batch processing multiple stress items."""
        user = UserFactory()
        items = [
            {"startTimeInSeconds": 1705276800, "timeOffsetStressLevelValues": {"0": 25, "300": 30}},
            {"startTimeInSeconds": 1705363200, "timeOffsetStressLevelValues": {"0": 40}},
        ]
        mock_bulk_create.side_effect = lambda db_session, samples: WriteCounts(len(samples), 0)

        count = garmin_247.process_items_batch(db, user.id, "stressDetails", items)

        mock_bulk_create.assert_called_once()
        assert count == 3  # 2 + 1

    @patch("app.services.event_record_service.event_record_service.bulk_create")
    @patch("app.services.event_record_service.event_record_service.bulk_create_details")
    def test_process_items_batch_sleeps(
        self,
        mock_bulk_details: MagicMock,
        mock_bulk_create: MagicMock,
        garmin_247: Garmin247Data,
        db: Session,
        sample_sleep: dict[str, Any],
    ) -> None:
        """Test batch processing sleep items uses bulk_create for records."""
        user = UserFactory()
        sleep2 = {**sample_sleep, "summaryId": "sleep_456"}

        # Make bulk_create return the actual record IDs that were passed in
        mock_bulk_create.side_effect = lambda db_session, records: [r.id for r in records]

        count = garmin_247.process_items_batch(db, user.id, "sleeps", [sample_sleep, sleep2])

        mock_bulk_create.assert_called_once()
        # Should create sleep details for inserted records
        mock_bulk_details.assert_called_once()
        assert count == 2

    def test_a_pushed_nights_score_is_linked_to_the_stored_night(self, garmin_247: Garmin247Data, db: Session) -> None:
        user = UserFactory()

        garmin_247.process_items_batch(db, user.id, "sleeps", [_SCORED_NIGHT])

        night = db.query(EventRecord).filter(EventRecord.external_id == "sleep_scored").one()
        scores = self._scores(db, user.id, HealthScoreCategory.SLEEP)
        assert [(s.event_record_id, s.value, s.qualifier) for s in scores] == [(night.id, Decimal("84"), "GOOD")]

    @pytest.mark.parametrize(
        "night",
        [
            {"overallSleepScore": {"value": 84, "qualifierKey": "GOOD"}, "sleepScoreQualifier": "FAIR"},
            {"overallSleepScore": {"value": 84, "qualifier": "GOOD"}, "sleepScoreQualifier": "FAIR"},
            {"overallSleepScore": {"value": 84}, "sleepScoreQualifier": "GOOD"},
        ],
        ids=["qualifierKey", "qualifier", "sleepScoreQualifier"],
    )
    def test_a_nights_qualifier_is_read_from_the_first_field_present(
        self, garmin_247: Garmin247Data, night: dict[str, Any]
    ) -> None:
        _, score = garmin_247.normalize_sleep({**_SCORED_NIGHT, **night}, uuid4())

        assert score is not None
        assert (score.value, score.qualifier) == (84, "GOOD")

    def test_a_re_sent_night_refreshes_its_one_score(self, garmin_247: Garmin247Data, db: Session) -> None:
        user = UserFactory()
        garmin_247.process_items_batch(db, user.id, "sleeps", [_SCORED_NIGHT])

        rescored = {**_SCORED_NIGHT, "overallSleepScore": {"value": 88, "qualifierKey": "EXCELLENT"}}
        garmin_247.process_items_batch(db, user.id, "sleeps", [rescored])

        night = db.query(EventRecord).filter(EventRecord.external_id == "sleep_scored").one()
        scores = self._scores(db, user.id, HealthScoreCategory.SLEEP)
        assert [(s.event_record_id, s.value, s.qualifier) for s in scores] == [(night.id, Decimal("88"), "EXCELLENT")]

    def test_a_re_sent_night_with_a_moved_end_stays_one_night_with_one_score(
        self, garmin_247: Garmin247Data, db: Session
    ) -> None:
        user = UserFactory()
        garmin_247.process_items_batch(db, user.id, "sleeps", [_SCORED_NIGHT])

        longer = {**_SCORED_NIGHT, "durationInSeconds": 30600, "overallSleepScore": {"value": 86}}
        garmin_247.process_items_batch(db, user.id, "sleeps", [longer])

        db.expire_all()
        night = db.query(EventRecord).filter(EventRecord.external_id == "sleep_scored").one()
        assert night.end_datetime == datetime(2024, 1, 15, 6, 30, tzinfo=timezone.utc)
        assert db.query(SleepDetails).filter(SleepDetails.record_id == night.id).one().sleep_time_in_bed_minutes == 510
        scores = self._scores(db, user.id, HealthScoreCategory.SLEEP)
        assert [(s.event_record_id, s.value, s.recorded_at) for s in scores] == [
            (night.id, Decimal("86"), night.end_datetime)
        ]

    def _store_a_second_copy(self, garmin_247: Garmin247Data, db: Session, user_id: UUID) -> None:
        """What the time-keyed insert used to leave behind when Garmin moved the night's end."""
        garmin_247.process_items_batch(db, user_id, "sleeps", [_SCORED_NIGHT])
        with patch.object(garmin_247.event_record_repo, "get_main_sleeps_by_external_ids", return_value={}):
            garmin_247.process_items_batch(db, user_id, "sleeps", [{**_SCORED_NIGHT, "durationInSeconds": 30600}])

    @staticmethod
    def _night_ends(db: Session) -> list[datetime]:
        db.expire_all()
        return sorted(r.end_datetime for r in db.query(EventRecord).filter(EventRecord.external_id == "sleep_scored"))

    def test_a_night_stored_twice_updates_its_latest_copy_and_leaves_the_other(
        self, garmin_247: Garmin247Data, db: Session
    ) -> None:
        user = UserFactory()
        self._store_a_second_copy(garmin_247, db, user.id)

        garmin_247.process_items_batch(db, user.id, "sleeps", [{**_SCORED_NIGHT, "durationInSeconds": 32400}])

        assert self._night_ends(db) == [
            datetime(2024, 1, 15, 6, tzinfo=timezone.utc),
            datetime(2024, 1, 15, 7, tzinfo=timezone.utc),
        ]

    def test_a_re_sent_night_whose_span_another_copy_holds_does_not_stop_the_push(
        self, garmin_247: Garmin247Data, db: Session
    ) -> None:
        user = UserFactory()
        self._store_a_second_copy(garmin_247, db, user.id)
        next_night = {**_SCORED_NIGHT, "summaryId": "sleep_next", "startTimeInSeconds": 1705356000}

        garmin_247.process_items_batch(db, user.id, "sleeps", [_SCORED_NIGHT, next_night])

        assert self._night_ends(db) == [
            datetime(2024, 1, 15, 6, tzinfo=timezone.utc),
            datetime(2024, 1, 15, 6, 30, tzinfo=timezone.utc),
        ]
        assert db.query(EventRecord).filter(EventRecord.external_id == "sleep_next").count() == 1

    def test_a_re_sent_night_queues_the_days_it_left_and_joined(self, garmin_247: Garmin247Data, db: Session) -> None:
        user = UserFactory()
        garmin_247.process_items_batch(db, user.id, "sleeps", [_SCORED_NIGHT])
        a_day_later = {**_SCORED_NIGHT, "startTimeInSeconds": _SCORED_NIGHT["startTimeInSeconds"] + 86400}

        with patch("app.services.event_record_service.mark_dirty_days") as mark_dirty_days:
            garmin_247.process_items_batch(db, user.id, "sleeps", [a_day_later])

        queued = {day for call in mark_dirty_days.call_args_list for _, day in call.args[1]}
        assert {date(2024, 1, 14), date(2024, 1, 15), date(2024, 1, 16)} <= queued

    def test_a_re_sent_night_keeps_its_nap_apart(self, garmin_247: Garmin247Data, db: Session) -> None:
        user = UserFactory()
        nap = {"napStartTimeInSeconds": 1705327200, "napDurationInSeconds": 1800, "napOffsetInSeconds": 3600}
        garmin_247.process_items_batch(db, user.id, "sleeps", [{**_SCORED_NIGHT, "naps": [nap]}])

        garmin_247.process_items_batch(
            db, user.id, "sleeps", [{**_SCORED_NIGHT, "durationInSeconds": 30600, "naps": [nap]}]
        )

        db.expire_all()
        spans = sorted(
            (r.start_datetime, r.end_datetime)
            for r in db.query(EventRecord).filter(EventRecord.external_id == "sleep_scored")
        )
        assert spans == [
            (datetime(2024, 1, 14, 22, tzinfo=timezone.utc), datetime(2024, 1, 15, 6, 30, tzinfo=timezone.utc)),
            (datetime(2024, 1, 15, 14, tzinfo=timezone.utc), datetime(2024, 1, 15, 14, 30, tzinfo=timezone.utc)),
        ]

    def test_a_score_stored_before_scores_were_linked_gets_linked(self, garmin_247: Garmin247Data, db: Session) -> None:
        user = UserFactory()
        _, unlinked = garmin_247.normalize_sleep(_SCORED_NIGHT, user.id)
        assert unlinked is not None
        db.add(HealthScore(**unlinked.model_dump()))
        db.flush()

        garmin_247.process_items_batch(db, user.id, "sleeps", [_SCORED_NIGHT])

        night = db.query(EventRecord).filter(EventRecord.external_id == "sleep_scored").one()
        assert [(s.id, s.event_record_id) for s in self._scores(db, user.id, HealthScoreCategory.SLEEP)] == [
            (unlinked.id, night.id)
        ]

    def test_a_nights_score_is_linked_to_the_night_not_its_nap(self, garmin_247: Garmin247Data, db: Session) -> None:
        user = UserFactory()
        nap = {"napStartTimeInSeconds": 1705327200, "napDurationInSeconds": 1800, "napOffsetInSeconds": 3600}

        garmin_247.process_items_batch(db, user.id, "sleeps", [{**_SCORED_NIGHT, "naps": [nap]}])

        night = (
            db.query(EventRecord)
            .filter(
                EventRecord.external_id == "sleep_scored",
                EventRecord.start_datetime == datetime(2024, 1, 14, 22, tzinfo=timezone.utc),
            )
            .one()
        )
        assert [s.event_record_id for s in self._scores(db, user.id, HealthScoreCategory.SLEEP)] == [night.id]

    def test_a_nights_in_sleep_aggregates_are_components_of_its_score(
        self, garmin_247: Garmin247Data, db: Session
    ) -> None:
        user = UserFactory()

        garmin_247.process_items_batch(db, user.id, "sleeps", [_SCORED_NIGHT])

        (score,) = self._scores(db, user.id, HealthScoreCategory.SLEEP)
        assert {name: c["value"] for name, c in score.components.items()} == {
            "average_heart_rate": 54,
            "lowest_heart_rate": 47,
            "respiration_avg": 14.2,
            "avg_oxygen_saturation": 95.5,
            "sleep_hrv": 48,
        }

    def test_an_unscored_nights_in_sleep_aggregates_are_kept_on_a_score_without_a_value(
        self, garmin_247: Garmin247Data, db: Session
    ) -> None:
        user = UserFactory()
        unscored = {key: value for key, value in _SCORED_NIGHT.items() if key != "overallSleepScore"}

        garmin_247.process_items_batch(db, user.id, "sleeps", [unscored])

        night = db.query(EventRecord).filter(EventRecord.external_id == "sleep_scored").one()
        (score,) = self._scores(db, user.id, HealthScoreCategory.SLEEP)
        assert (score.event_record_id, score.value, score.qualifier) == (night.id, None, None)
        assert {name: c["value"] for name, c in score.components.items()} == {
            "average_heart_rate": 54,
            "lowest_heart_rate": 47,
            "respiration_avg": 14.2,
            "avg_oxygen_saturation": 95.5,
            "sleep_hrv": 48,
        }

    def test_a_night_without_a_score_or_aggregates_has_no_score(self, garmin_247: Garmin247Data, db: Session) -> None:
        user = UserFactory()
        bare = {key: _SCORED_NIGHT[key] for key in ("summaryId", "startTimeInSeconds", "durationInSeconds")}

        garmin_247.process_items_batch(db, user.id, "sleeps", [bare])

        assert self._scores(db, user.id, HealthScoreCategory.SLEEP) == []

    def test_a_night_without_a_summary_id_keeps_its_score_unlinked(
        self, garmin_247: Garmin247Data, db: Session
    ) -> None:
        user = UserFactory()
        without_id = {key: value for key, value in _SCORED_NIGHT.items() if key != "summaryId"}

        garmin_247.process_items_batch(db, user.id, "sleeps", [without_id])

        assert [(s.event_record_id, s.value) for s in self._scores(db, user.id, HealthScoreCategory.SLEEP)] == [
            (None, Decimal("84"))
        ]

    def test_a_night_whose_span_another_record_holds_keeps_its_score_unlinked(
        self, garmin_247: Garmin247Data, db: Session
    ) -> None:
        user = UserFactory()
        bare = {key: _SCORED_NIGHT[key] for key in ("startTimeInSeconds", "durationInSeconds")}
        garmin_247.process_items_batch(db, user.id, "sleeps", [{**bare, "summaryId": "sleep_other"}])

        garmin_247.process_items_batch(db, user.id, "sleeps", [_SCORED_NIGHT])

        assert [(s.event_record_id, s.value) for s in self._scores(db, user.id, HealthScoreCategory.SLEEP)] == [
            (None, Decimal("84"))
        ]

    def test_a_night_that_builds_no_record_keeps_its_score_unlinked(
        self, garmin_247: Garmin247Data, db: Session
    ) -> None:
        user = UserFactory()

        with patch.object(garmin_247, "_build_sleep_record", return_value=None):
            garmin_247.process_items_batch(db, user.id, "sleeps", [_SCORED_NIGHT])

        assert [(s.event_record_id, s.value) for s in self._scores(db, user.id, HealthScoreCategory.SLEEP)] == [
            (None, Decimal("84"))
        ]

    def test_a_score_the_database_rejects_does_not_stop_the_push(self, garmin_247: Garmin247Data, db: Session) -> None:
        user = UserFactory()
        next_night = {**_SCORED_NIGHT, "summaryId": "sleep_next", "startTimeInSeconds": 1705356000}
        upsert = health_score_service.upsert_event_score

        def reject_the_first_night(db_session: Session, score: HealthScoreCreate) -> HealthScore:
            if score.recorded_at == datetime(2024, 1, 15, 6, tzinfo=timezone.utc):
                # A night that is not stored: the flush violates the score's foreign key
                score = score.model_copy(update={"event_record_id": uuid4()})
            return upsert(db_session, score)

        with patch.object(health_score_service, "upsert_event_score", side_effect=reject_the_first_night):
            garmin_247.process_items_batch(db, user.id, "sleeps", [_SCORED_NIGHT, next_night])

        nights = {r.external_id: r.id for r in db.query(EventRecord).filter(EventRecord.category == "sleep")}
        assert set(nights) == {"sleep_scored", "sleep_next"}
        assert [s.event_record_id for s in self._scores(db, user.id, HealthScoreCategory.SLEEP)] == [
            nights["sleep_next"]
        ]

    def test_a_nights_spo2_and_respiration_are_samples_at_their_offsets(
        self, garmin_247: Garmin247Data, db: Session
    ) -> None:
        user = UserFactory()

        garmin_247.process_items_batch(db, user.id, "sleeps", [_SCORED_NIGHT])

        stored = (
            db.query(SeriesSample.series_type_definition_id, SeriesSample.recorded_at, SeriesSample.value)
            .join(DataSource, DataSource.id == SeriesSample.data_source_id)
            .filter(DataSource.user_id == user.id)
            .all()
        )
        spo2, breaths = (
            get_series_type_id(SeriesType.oxygen_saturation),
            get_series_type_id(SeriesType.respiratory_rate),
        )
        assert sorted((t, at, float(v)) for t, at, v in stored) == sorted(
            [
                (spo2, datetime(2024, 1, 14, 22, tzinfo=timezone.utc), 95.0),
                (spo2, datetime(2024, 1, 14, 23, tzinfo=timezone.utc), 93.0),
                (breaths, datetime(2024, 1, 14, 22, tzinfo=timezone.utc), 14.5),
                (breaths, datetime(2024, 1, 14, 22, 30, tzinfo=timezone.utc), 13.0),
            ]
        )

    def test_normalize_sleep_includes_naps(self, garmin_247: Garmin247Data) -> None:
        """normalize_sleep passes through the naps list and sets is_nap=False."""
        user_id = uuid4()
        raw = {
            "summaryId": "sleep_with_nap",
            "startTimeInSeconds": 1705273200,
            "durationInSeconds": 28800,
            "naps": [
                {
                    "napDurationInSeconds": 600,
                    "napStartTimeInSeconds": 1690916700,
                    "napValidation": "DEVICE",
                    "napOffsetInSeconds": -18000,
                }
            ],
        }
        normalized, _ = garmin_247.normalize_sleep(raw, user_id)
        assert normalized["is_nap"] is False
        assert len(normalized["naps"]) == 1

    def test_build_nap_record(self, garmin_247: Garmin247Data) -> None:
        """_build_nap_record returns a record+detail with is_nap=True and correct fields."""
        user_id = uuid4()
        nap = {
            "napDurationInSeconds": 600,
            "napStartTimeInSeconds": 1690916700,
            "napValidation": "DEVICE",
            "napOffsetInSeconds": -18000,
        }
        result = garmin_247._build_nap_record(nap, user_id, parent_summary_id="sleep_123")
        assert result is not None
        record, detail = result
        assert record.external_id == "sleep_123"
        assert record.duration_seconds == 600
        assert detail.is_nap is True
        assert detail.sleep_total_duration_minutes == 10  # 600 // 60

    def test_build_nap_record_missing_fields_returns_none(self, garmin_247: Garmin247Data) -> None:
        """_build_nap_record returns None when start time or duration is missing."""
        user_id = uuid4()
        assert garmin_247._build_nap_record({}, user_id) is None
        assert garmin_247._build_nap_record({"napDurationInSeconds": 600}, user_id) is None
        assert garmin_247._build_nap_record({"napStartTimeInSeconds": 1690916700}, user_id) is None

    @patch("app.services.event_record_service.event_record_service.bulk_create")
    @patch("app.services.event_record_service.event_record_service.bulk_create_details")
    def test_process_items_batch_sleeps_with_naps(
        self,
        mock_bulk_details: MagicMock,
        mock_bulk_create: MagicMock,
        garmin_247: Garmin247Data,
        db: Session,
        sample_sleep: dict[str, Any],
    ) -> None:
        """Naps embedded in a sleep summary are saved as separate is_nap=True records."""
        user = UserFactory()
        sleep_with_nap = {
            **sample_sleep,
            "naps": [
                {
                    "napDurationInSeconds": 600,
                    "napStartTimeInSeconds": 1690916700,
                    "napValidation": "DEVICE",
                    "napOffsetInSeconds": -18000,
                }
            ],
        }
        mock_bulk_create.side_effect = lambda db_session, records: [r.id for r in records]

        count = garmin_247.process_items_batch(db, user.id, "sleeps", [sleep_with_nap])

        # 1 main sleep + 1 nap
        assert count == 2
        all_records = mock_bulk_create.call_args[0][1]
        assert len(all_records) == 2

    def test_skin_temp_deviation_becomes_a_sample(self, garmin_247: Garmin247Data) -> None:
        # The skinTemp payload carries a deviation from baseline, never an absolute reading.
        raw = {
            "summaryId": "x60a3665-6abad64b",
            "calendarDate": "2026-09-29",
            "avgDeviationCelsius": -0.1,
            "durationInSeconds": 31200,
            "startTimeInSeconds": 1790629451,
            "startTimeOffsetInSeconds": 7200,
        }

        samples = garmin_247._build_skin_temp_samples(uuid4(), raw)

        assert [sample.series_type for sample in samples] == [SeriesType.skin_temperature_deviation]
        assert float(samples[0].value) == -0.1
        assert samples[0].external_id == "x60a3665-6abad64b"

    def test_a_night_measured_before_midnight_keeps_garmins_own_day(self, garmin_247: Garmin247Data) -> None:
        # Measurement starts at 23:02 local on the 23rd; Garmin calls that night the 24th.
        raw = {
            "summaryId": "x60a3665-late",
            "calendarDate": "2026-09-24",
            "avgDeviationCelsius": 0.3,
            "durationInSeconds": 25200,
            "startTimeInSeconds": 1790197320,
            "startTimeOffsetInSeconds": 7200,
        }

        samples = garmin_247._build_skin_temp_samples(uuid4(), raw)

        assert samples[0].recorded_at.date().isoformat() == "2026-09-24"

    def test_process_items_batch_empty(self, garmin_247: Garmin247Data, db: Session) -> None:
        """Test batch processing empty items returns 0."""
        user = UserFactory()
        count = garmin_247.process_items_batch(db, user.id, "dailies", [])
        assert count == 0

    @patch("app.repositories.data_point_series_repository.DataPointSeriesRepository.bulk_create")
    def test_process_items_batch_skips_bad_items(
        self,
        mock_bulk_create: MagicMock,
        garmin_247: Garmin247Data,
        db: Session,
    ) -> None:
        """Test batch processing skips invalid items and continues."""
        user = UserFactory()
        items = [
            {"startTimeInSeconds": 0},  # Missing timestamp -> skipped
            {"startTimeInSeconds": 1705276800, "timeOffsetStressLevelValues": {"0": 50}},  # Valid
        ]
        mock_bulk_create.side_effect = lambda db_session, samples: WriteCounts(len(samples), 0)

        count = garmin_247.process_items_batch(db, user.id, "stressDetails", items)

        mock_bulk_create.assert_called_once()
        assert count == 1


class TestWellnessPushRun:
    def test_push_is_written_under_the_run_it_reports(self) -> None:
        connection = MagicMock(user_id=uuid4())
        connection_repo = MagicMock()
        connection_repo.get_all_by_provider_user_id.return_value = [connection]
        written_under = []
        garmin_247 = MagicMock()
        garmin_247.process_items_batch.side_effect = lambda *_args: written_under.append(sync_run_var.get()) or 1

        with patch("app.services.providers.garmin.handlers.wellness.emit_sync_completed") as completed:
            process_wellness_items(
                MagicMock(), connection_repo, garmin_247, "dailies", [{"userId": "g1"}], [], set(), "trace"
            )

        assert written_under[0].run_id == completed.call_args.kwargs["run_id"]
        assert (written_under[0].source, written_under[0].scope) == (SyncSource.WEBHOOK, SyncScope.LIVE)
        assert sync_run_var.get() is None
