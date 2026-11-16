"""
Integration tests for Ultrahuman provider using mocked API calls.

These tests verify the complete Ultrahuman integration from API response
to database storage using mocked responses, making them deterministic
and runnable without real credentials.
"""

import copy
from datetime import datetime, timezone
from typing import Any
from unittest.mock import patch
from uuid import UUID

from fastapi.testclient import TestClient
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.models import DataSource, EventRecord, SeriesAggregate, SeriesSample, SeriesTypeDefinition, SleepDetails
from app.schemas.enums.series_types import SeriesType
from app.services.providers.factory import ProviderFactory
from app.services.providers.ultrahuman.data_247 import Ultrahuman247Data
from tests.factories import (
    ApiKeyFactory,
    DataSourceFactory,
    UserConnectionFactory,
    UserFactory,
)
from tests.utils import api_key_headers


class TestUltrahumanSleepDataIntegration:
    """Integration tests for Ultrahuman sleep data synchronization."""

    def test_full_sleep_sync_flow_with_mocked_api(self, db: Session, sample_ultrahuman_api_response: dict) -> None:
        """Test complete sleep sync flow: Mocked API -> Normalization -> Database."""
        user = UserFactory()
        UserConnectionFactory(user=user, provider="ultrahuman", status="active", access_token="test_token")
        DataSourceFactory(user_id=user.id, provider="ultrahuman")

        factory = ProviderFactory()
        strategy = factory.get_provider("ultrahuman")
        provider_impl = strategy.data_247
        assert isinstance(provider_impl, Ultrahuman247Data)

        end_time = datetime(2024, 1, 16, tzinfo=timezone.utc)
        start_time = datetime(2024, 1, 15, tzinfo=timezone.utc)

        with patch.object(
            provider_impl,
            "_make_api_request",
            return_value=sample_ultrahuman_api_response,
        ) as mock_request:
            results = provider_impl.load_and_save_all(db, user.id, start_time=start_time, end_time=end_time)
            db.commit()

            assert mock_request.call_count == 2  # 2 days in range
            assert results is not None
            assert "sleep" in results.outcomes
            assert results.outcomes["sleep"].rows_written >= 0

    def test_verify_sleep_records_in_database_with_mocked_api(
        self, db: Session, sample_ultrahuman_api_response: dict
    ) -> None:
        """Verify sleep records are correctly stored with all fields."""
        user = UserFactory()
        UserConnectionFactory(user=user, provider="ultrahuman", status="active", access_token="test_token")
        DataSourceFactory(user_id=user.id, provider="ultrahuman")

        factory = ProviderFactory()
        strategy = factory.get_provider("ultrahuman")
        provider_impl = strategy.data_247
        assert isinstance(provider_impl, Ultrahuman247Data)

        end_time = datetime(2024, 1, 16, tzinfo=timezone.utc)
        start_time = datetime(2024, 1, 15, tzinfo=timezone.utc)

        with patch.object(provider_impl, "_make_api_request", return_value=sample_ultrahuman_api_response):
            results = provider_impl.load_and_save_all(db, user.id, start_time=start_time, end_time=end_time)
            db.commit()

            assert results.outcomes["sleep"].rows_written > 0, "Fixture has sleep data, expected synced sessions"
            records = (
                db.query(EventRecord)
                .join(DataSource)
                .filter(
                    DataSource.user_id == user.id,
                    DataSource.provider == "ultrahuman",
                    EventRecord.category == "sleep",
                )
                .all()
            )

            assert len(records) > 0, "No sleep records found in database"

            for record in records:
                assert record.category == "sleep"
                assert record.duration_seconds is not None
                assert record.start_datetime is not None
                assert record.end_datetime is not None

                details = db.query(SleepDetails).filter(SleepDetails.record_id == record.id).first()
                assert details is not None, f"SleepDetails missing for record {record.id}"
                assert details.sleep_efficiency_score is not None
                assert details.sleep_deep_minutes is not None
                assert details.sleep_light_minutes is not None
                assert details.sleep_rem_minutes is not None
                assert details.sleep_awake_minutes is not None

                total = (
                    (details.sleep_deep_minutes or 0)
                    + (details.sleep_light_minutes or 0)
                    + (details.sleep_rem_minutes or 0)
                    + (details.sleep_awake_minutes or 0)
                )
                total_sleep = (
                    (details.sleep_deep_minutes or 0)
                    + (details.sleep_light_minutes or 0)
                    + (details.sleep_rem_minutes or 0)
                )

                assert total >= 0, "Total minutes should be non-negative"
                assert total_sleep >= 0, "Total sleep minutes should be non-negative"

    def test_sleep_efficiency_extraction_with_mocked_api(
        self, db: Session, sample_ultrahuman_api_response: dict
    ) -> None:
        """Verify sleep efficiency is correctly extracted from quick_metrics."""
        user = UserFactory()
        UserConnectionFactory(user=user, provider="ultrahuman", status="active", access_token="test_token")
        DataSourceFactory(user_id=user.id, provider="ultrahuman")

        factory = ProviderFactory()
        strategy = factory.get_provider("ultrahuman")
        provider_impl = strategy.data_247
        assert isinstance(provider_impl, Ultrahuman247Data)

        end_time = datetime(2024, 1, 16, tzinfo=timezone.utc)
        start_time = datetime(2024, 1, 15, tzinfo=timezone.utc)

        with patch.object(provider_impl, "_make_api_request", return_value=sample_ultrahuman_api_response):
            results = provider_impl.load_and_save_all(db, user.id, start_time=start_time, end_time=end_time)
            db.commit()

            assert results.outcomes["sleep"].rows_written > 0, "Fixture has sleep data, expected synced sessions"
            records = (
                db.query(EventRecord)
                .join(DataSource)
                .filter(
                    DataSource.user_id == user.id,
                    EventRecord.category == "sleep",
                )
                .all()
            )

            for record in records:
                details = db.query(SleepDetails).filter(SleepDetails.record_id == record.id).first()
                assert details is not None, f"SleepDetails missing for record {record.id}"
                assert details.sleep_efficiency_score is not None, "Sleep efficiency should not be null"
                assert 0 <= details.sleep_efficiency_score <= 100, (
                    f"Sleep efficiency {details.sleep_efficiency_score} should be 0-100"
                )

    def test_sleep_stage_values_are_nonzero_with_mocked_api(
        self, db: Session, sample_ultrahuman_api_response: dict
    ) -> None:
        """Verify sleep stage values are not all zero after sync."""
        user = UserFactory()
        UserConnectionFactory(user=user, provider="ultrahuman", status="active", access_token="test_token")
        DataSourceFactory(user_id=user.id, provider="ultrahuman")

        factory = ProviderFactory()
        strategy = factory.get_provider("ultrahuman")
        provider_impl = strategy.data_247
        assert isinstance(provider_impl, Ultrahuman247Data)

        end_time = datetime(2024, 1, 16, tzinfo=timezone.utc)
        start_time = datetime(2024, 1, 15, tzinfo=timezone.utc)

        with patch.object(provider_impl, "_make_api_request", return_value=sample_ultrahuman_api_response):
            results = provider_impl.load_and_save_all(db, user.id, start_time=start_time, end_time=end_time)
            db.commit()

            assert results.outcomes["sleep"].rows_written > 0, "Fixture has sleep data, expected synced sessions"
            records = (
                db.query(EventRecord)
                .join(DataSource)
                .filter(
                    DataSource.user_id == user.id,
                    EventRecord.category == "sleep",
                )
                .all()
            )

            all_zero = True
            for record in records:
                details = db.query(SleepDetails).filter(SleepDetails.record_id == record.id).first()
                assert details is not None, f"SleepDetails missing for record {record.id}"
                stage_sum = (
                    (details.sleep_deep_minutes or 0)
                    + (details.sleep_light_minutes or 0)
                    + (details.sleep_rem_minutes or 0)
                    + (details.sleep_awake_minutes or 0)
                )
                if stage_sum > 0:
                    all_zero = False
                    break

            assert not all_zero, "All sleep stage values are zero - parsing may be broken"

    def test_sleep_graph_stages_persisted_to_jsonb_with_mocked_api(
        self, db: Session, sample_ultrahuman_api_response: dict
    ) -> None:
        """Verify sleep_graph.data intervals are normalized and stored in sleep_details.sleep_stages."""
        user = UserFactory()
        UserConnectionFactory(user=user, provider="ultrahuman", status="active", access_token="test_token")
        DataSourceFactory(user_id=user.id, provider="ultrahuman")

        factory = ProviderFactory()
        strategy = factory.get_provider("ultrahuman")
        provider_impl = strategy.data_247
        assert isinstance(provider_impl, Ultrahuman247Data)

        end_time = datetime(2024, 1, 16, tzinfo=timezone.utc)
        start_time = datetime(2024, 1, 15, tzinfo=timezone.utc)

        with patch.object(provider_impl, "_make_api_request", return_value=sample_ultrahuman_api_response):
            provider_impl.load_and_save_all(db, user.id, start_time=start_time, end_time=end_time)
            db.commit()

        details = (
            db.query(SleepDetails)
            .join(EventRecord, SleepDetails.record_id == EventRecord.id)
            .join(DataSource)
            .filter(DataSource.user_id == user.id, EventRecord.category == "sleep")
            .all()
        )

        with_stages = [d for d in details if d.sleep_stages]
        assert with_stages, "Expected at least one sleep record with sleep_stages populated"

        for detail in with_stages:
            stages = detail.sleep_stages
            # Stored as a JSONB list of {stage, start_time, end_time} with ISO 8601 strings.
            assert isinstance(stages, list)
            assert len(stages) > 0
            for interval in stages:
                assert set(interval) >= {"stage", "start_time", "end_time"}
                assert isinstance(interval["start_time"], str)  # unix ts → ISO 8601 string
                assert "T" in interval["start_time"]
            assert {i["stage"] for i in stages} <= {"awake", "light", "deep", "rem", "unknown"}


class TestUltrahumanActivitySamplesIntegration:
    """Integration tests for Ultrahuman activity samples synchronization."""

    def test_full_activity_samples_sync_flow_with_mocked_api(
        self, db: Session, sample_ultrahuman_api_response: dict
    ) -> None:
        """Test complete activity samples sync flow: Mocked API -> Normalization -> Database."""
        user = UserFactory()
        UserConnectionFactory(user=user, provider="ultrahuman", status="active", access_token="test_token")
        DataSourceFactory(user_id=user.id, provider="ultrahuman")

        factory = ProviderFactory()
        strategy = factory.get_provider("ultrahuman")
        provider_impl = strategy.data_247
        assert isinstance(provider_impl, Ultrahuman247Data)

        end_time = datetime(2024, 1, 16, tzinfo=timezone.utc)
        start_time = datetime(2024, 1, 15, tzinfo=timezone.utc)

        with patch.object(provider_impl, "_make_api_request", return_value=sample_ultrahuman_api_response):
            results = provider_impl.load_and_save_all(db, user.id, start_time=start_time, end_time=end_time)
            db.commit()

            assert results is not None
            assert "activity_samples" in results.outcomes
            assert results.outcomes["activity_samples"].rows_written >= 0

    def test_verify_activity_samples_in_database_with_mocked_api(
        self, db: Session, sample_ultrahuman_api_response: dict
    ) -> None:
        """Verify activity samples are correctly stored for all data types."""
        user = UserFactory()
        UserConnectionFactory(user=user, provider="ultrahuman", status="active", access_token="test_token")
        DataSourceFactory(user_id=user.id, provider="ultrahuman")

        factory = ProviderFactory()
        strategy = factory.get_provider("ultrahuman")
        provider_impl = strategy.data_247
        assert isinstance(provider_impl, Ultrahuman247Data)

        end_time = datetime(2024, 1, 16, tzinfo=timezone.utc)
        start_time = datetime(2024, 1, 15, tzinfo=timezone.utc)

        with patch.object(provider_impl, "_make_api_request", return_value=sample_ultrahuman_api_response):
            results = provider_impl.load_and_save_all(db, user.id, start_time=start_time, end_time=end_time)
            db.commit()

            assert results.outcomes["activity_samples"].rows_written > 0, (
                "Fixture has activity data, expected synced samples"
            )

            samples = (
                db.query(SeriesSample)
                .join(DataSource)
                .filter(
                    DataSource.user_id == user.id,
                    DataSource.provider == "ultrahuman",
                )
                .all()
            )

            assert len(samples) > 0, "No activity samples found in database"

            # Collect series type codes present in synced samples
            type_id_to_code = {}
            for sample in samples:
                if sample.series_type_definition_id not in type_id_to_code:
                    std = db.query(SeriesTypeDefinition).get(sample.series_type_definition_id)
                    type_id_to_code[sample.series_type_definition_id] = std.code if std else None

            synced_codes = set(type_id_to_code.values())
            expected_codes = {"heart_rate", "heart_rate_variability_rmssd", "skin_temperature", "steps"}

            for code in expected_codes:
                assert code in synced_codes, f"{code} missing from synced samples"

    def test_daily_scalars_are_mirrored_on_the_requested_day(
        self, db: Session, sample_ultrahuman_api_response: dict
    ) -> None:
        user = UserFactory()
        UserConnectionFactory(user=user, provider="ultrahuman", status="active", access_token="test_token")
        provider_impl = ProviderFactory().get_provider("ultrahuman").data_247
        assert isinstance(provider_impl, Ultrahuman247Data)

        with patch.object(provider_impl, "_make_api_request", return_value=sample_ultrahuman_api_response):
            provider_impl.load_and_save_all(
                db,
                user.id,
                start_time=datetime(2024, 1, 15, tzinfo=timezone.utc),
                end_time=datetime(2024, 1, 16, tzinfo=timezone.utc),
            )
            db.commit()

        vo2 = (
            db.query(SeriesAggregate)
            .join(SeriesTypeDefinition, SeriesAggregate.series_type_definition_id == SeriesTypeDefinition.id)
            .filter(SeriesTypeDefinition.code == SeriesType.vo2_max.value)
            .all()
        )
        assert vo2
        assert {(a.period_start, a.zone_offset) for a in vo2} <= {
            (datetime(2024, 1, 15, tzinfo=timezone.utc), "+00:00"),
            (datetime(2024, 1, 16, tzinfo=timezone.utc), "+00:00"),
        }

    def test_vo2_max_saved_with_mocked_api(self, db: Session, sample_ultrahuman_api_response: dict) -> None:
        """Verify VO2 max value is saved as a SeriesAggregate after sync."""
        user = UserFactory()
        UserConnectionFactory(user=user, provider="ultrahuman", status="active", access_token="test_token")
        DataSourceFactory(user_id=user.id, provider="ultrahuman")

        factory = ProviderFactory()
        strategy = factory.get_provider("ultrahuman")
        provider_impl = strategy.data_247
        assert isinstance(provider_impl, Ultrahuman247Data)

        end_time = datetime(2024, 1, 16, tzinfo=timezone.utc)
        start_time = datetime(2024, 1, 15, tzinfo=timezone.utc)

        with patch.object(provider_impl, "_make_api_request", return_value=sample_ultrahuman_api_response):
            provider_impl.load_and_save_all(db, user.id, start_time=start_time, end_time=end_time)
            db.commit()

        samples = (
            db.query(SeriesAggregate)
            .join(DataSource)
            .join(SeriesTypeDefinition, SeriesAggregate.series_type_definition_id == SeriesTypeDefinition.id)
            .filter(
                DataSource.user_id == user.id,
                SeriesTypeDefinition.code == SeriesType.vo2_max.value,
            )
            .all()
        )

        assert len(samples) > 0, "No VO2 max samples found"
        for sample in samples:
            value = float(sample.sum if sample.sum is not None else sample.avg)
            assert 15 <= value <= 90, f"VO2 max {value} is outside realistic range (15-90 ml/kg/min)"

    def test_temperature_deviation_saved_as_skin_temperature_deviation(
        self, db: Session, sample_ultrahuman_api_response: dict
    ) -> None:
        """The ring's nightly deviation from baseline reaches the skin_temperature_deviation series."""
        user = UserFactory()
        UserConnectionFactory(user=user, provider="ultrahuman", status="active", access_token="test_token")
        DataSourceFactory(user_id=user.id, provider="ultrahuman")

        provider_impl = ProviderFactory().get_provider("ultrahuman").data_247
        assert isinstance(provider_impl, Ultrahuman247Data)

        with patch.object(provider_impl, "_make_api_request", return_value=sample_ultrahuman_api_response):
            provider_impl.load_and_save_all(
                db,
                user.id,
                start_time=datetime(2024, 1, 15, tzinfo=timezone.utc),
                end_time=datetime(2024, 1, 16, tzinfo=timezone.utc),
            )
            db.commit()

        samples = (
            db.query(SeriesAggregate)
            .join(DataSource)
            .join(SeriesTypeDefinition, SeriesAggregate.series_type_definition_id == SeriesTypeDefinition.id)
            .filter(
                DataSource.user_id == user.id,
                SeriesTypeDefinition.code == SeriesType.skin_temperature_deviation.value,
            )
            .all()
        )

        # The mock answers both requested days with the same day_start, and a day row sits on the day the
        # ring reports, so the two answers are one day.
        assert [float(sample.sum if sample.sum is not None else sample.avg) for sample in samples] == [-0.2]

    def test_sleep_rhr_saved_as_resting_heart_rate(self, db: Session, sample_ultrahuman_api_response: dict) -> None:
        """The ring's overnight resting heart rate reaches the resting_heart_rate series."""
        user = UserFactory()
        UserConnectionFactory(user=user, provider="ultrahuman", status="active", access_token="test_token")
        DataSourceFactory(user_id=user.id, provider="ultrahuman")

        provider_impl = ProviderFactory().get_provider("ultrahuman").data_247
        assert isinstance(provider_impl, Ultrahuman247Data)

        with patch.object(provider_impl, "_make_api_request", return_value=sample_ultrahuman_api_response):
            provider_impl.load_and_save_all(
                db,
                user.id,
                start_time=datetime(2024, 1, 15, tzinfo=timezone.utc),
                end_time=datetime(2024, 1, 16, tzinfo=timezone.utc),
            )
            db.commit()

        samples = (
            db.query(SeriesAggregate)
            .join(DataSource)
            .join(SeriesTypeDefinition, SeriesAggregate.series_type_definition_id == SeriesTypeDefinition.id)
            .filter(
                DataSource.user_id == user.id,
                SeriesTypeDefinition.code == SeriesType.resting_heart_rate.value,
            )
            .all()
        )

        # The mock answers both requested days with the same day_start: one day, on the day the ring reports.
        assert [float(sample.sum if sample.sum is not None else sample.avg) for sample in samples] == [51]
        assert samples[0].period_start == datetime(2024, 1, 15, 0, 0, tzinfo=timezone.utc)

    def test_active_minutes_saved_as_exercise_time(self, db: Session, sample_ultrahuman_api_response: dict) -> None:
        """Ultrahuman's active minutes are moderate to vigorous activity, not all non-sedentary time."""
        user = UserFactory()
        UserConnectionFactory(user=user, provider="ultrahuman", status="active", access_token="test_token")
        DataSourceFactory(user_id=user.id, provider="ultrahuman")

        provider_impl = ProviderFactory().get_provider("ultrahuman").data_247
        assert isinstance(provider_impl, Ultrahuman247Data)

        with patch.object(provider_impl, "_make_api_request", return_value=sample_ultrahuman_api_response):
            provider_impl.load_and_save_all(
                db,
                user.id,
                start_time=datetime(2024, 1, 15, tzinfo=timezone.utc),
                end_time=datetime(2024, 1, 16, tzinfo=timezone.utc),
            )
            db.commit()

        codes_and_values = (
            db.query(SeriesTypeDefinition.code, func.coalesce(SeriesAggregate.sum, SeriesAggregate.avg))
            .select_from(SeriesAggregate)
            .join(DataSource, SeriesAggregate.data_source_id == DataSource.id)
            .join(SeriesTypeDefinition, SeriesAggregate.series_type_definition_id == SeriesTypeDefinition.id)
            .filter(
                DataSource.user_id == user.id,
                SeriesTypeDefinition.code.in_([SeriesType.exercise_time.value, SeriesType.active_time.value]),
            )
            .all()
        )

        assert [(code, float(value)) for code, value in codes_and_values] == [(SeriesType.exercise_time.value, 35)]

    def test_heart_rate_values_are_reasonable_with_mocked_api(
        self, db: Session, sample_ultrahuman_api_response: dict
    ) -> None:
        """Verify heart rate values are within realistic range (40-200 bpm)."""
        user = UserFactory()
        UserConnectionFactory(user=user, provider="ultrahuman", status="active", access_token="test_token")
        DataSourceFactory(user_id=user.id, provider="ultrahuman")

        factory = ProviderFactory()
        strategy = factory.get_provider("ultrahuman")
        provider_impl = strategy.data_247
        assert isinstance(provider_impl, Ultrahuman247Data)

        end_time = datetime(2024, 1, 16, tzinfo=timezone.utc)
        start_time = datetime(2024, 1, 15, tzinfo=timezone.utc)

        with patch.object(provider_impl, "_make_api_request", return_value=sample_ultrahuman_api_response):
            provider_impl.load_and_save_all(db, user.id, start_time=start_time, end_time=end_time)
            db.commit()

        samples = (
            db.query(SeriesSample)
            .join(DataSource)
            .join(SeriesTypeDefinition, SeriesSample.series_type_definition_id == SeriesTypeDefinition.id)
            .filter(
                DataSource.user_id == user.id,
                SeriesTypeDefinition.code == SeriesType.heart_rate.value,
            )
            .all()
        )

        assert len(samples) > 0, "No heart rate samples found"
        for sample in samples:
            value = float(sample.value)
            assert 40 <= value <= 200, f"Heart rate {value} is outside realistic range"

    def test_temperature_values_are_reasonable_with_mocked_api(
        self, db: Session, sample_ultrahuman_api_response: dict
    ) -> None:
        """Verify skin temperature values are within realistic range."""
        user = UserFactory()
        UserConnectionFactory(user=user, provider="ultrahuman", status="active", access_token="test_token")
        DataSourceFactory(user_id=user.id, provider="ultrahuman")

        factory = ProviderFactory()
        strategy = factory.get_provider("ultrahuman")
        provider_impl = strategy.data_247
        assert isinstance(provider_impl, Ultrahuman247Data)

        end_time = datetime(2024, 1, 16, tzinfo=timezone.utc)
        start_time = datetime(2024, 1, 15, tzinfo=timezone.utc)

        with patch.object(provider_impl, "_make_api_request", return_value=sample_ultrahuman_api_response):
            provider_impl.load_and_save_all(db, user.id, start_time=start_time, end_time=end_time)
            db.commit()

        samples = (
            db.query(SeriesSample)
            .join(DataSource)
            .join(SeriesTypeDefinition, SeriesSample.series_type_definition_id == SeriesTypeDefinition.id)
            .filter(
                DataSource.user_id == user.id,
                SeriesTypeDefinition.code == SeriesType.skin_temperature.value,
            )
            .all()
        )

        assert len(samples) > 0, "No temperature samples found"
        for sample in samples:
            value = float(sample.value)
            assert 29 <= value <= 42, f"Temperature {value} is outside realistic range"

    def test_timestamps_are_utc_with_mocked_api(self, db: Session, sample_ultrahuman_api_response: dict) -> None:
        """Verify all activity sample timestamps are in UTC timezone."""
        user = UserFactory()
        UserConnectionFactory(user=user, provider="ultrahuman", status="active", access_token="test_token")
        DataSourceFactory(user_id=user.id, provider="ultrahuman")

        factory = ProviderFactory()
        strategy = factory.get_provider("ultrahuman")
        provider_impl = strategy.data_247
        assert isinstance(provider_impl, Ultrahuman247Data)

        end_time = datetime(2024, 1, 16, tzinfo=timezone.utc)
        start_time = datetime(2024, 1, 15, tzinfo=timezone.utc)

        with patch.object(provider_impl, "_make_api_request", return_value=sample_ultrahuman_api_response):
            provider_impl.load_and_save_all(db, user.id, start_time=start_time, end_time=end_time)
            db.commit()

        samples = db.query(SeriesSample).join(DataSource).filter(DataSource.user_id == user.id).all()

        assert len(samples) > 0, "No activity samples found"
        for sample in samples:
            assert sample.recorded_at.tzinfo is not None, f"Timestamp {sample.recorded_at} has no timezone info"
            assert sample.recorded_at.utcoffset().total_seconds() == 0, f"Timestamp {sample.recorded_at} is not UTC"


class TestUltrahumanAPIEndpoints:
    """Tests for Ultrahuman-specific API endpoints."""

    def test_sleep_events_endpoint_returns_data_with_mocked_api(
        self, db: Session, sample_ultrahuman_api_response: dict
    ) -> None:
        """Verify sleep events endpoint returns data after sync."""
        user = UserFactory()
        UserConnectionFactory(user=user, provider="ultrahuman", status="active", access_token="test_token")
        DataSourceFactory(user_id=user.id, provider="ultrahuman")

        factory = ProviderFactory()
        strategy = factory.get_provider("ultrahuman")
        provider_impl = strategy.data_247
        assert isinstance(provider_impl, Ultrahuman247Data)

        end_time = datetime(2024, 1, 16, tzinfo=timezone.utc)
        start_time = datetime(2024, 1, 15, tzinfo=timezone.utc)

        with patch.object(provider_impl, "_make_api_request", return_value=sample_ultrahuman_api_response):
            results = provider_impl.load_and_save_all(db, user.id, start_time=start_time, end_time=end_time)
            db.commit()

            assert results.outcomes["sleep"].rows_written > 0, "Fixture has sleep data, expected synced sessions"
            records = (
                db.query(EventRecord)
                .join(DataSource)
                .filter(
                    DataSource.user_id == user.id,
                    EventRecord.category == "sleep",
                )
                .all()
            )

            assert len(records) > 0, "Should have sleep records after sync"

    def test_timeseries_endpoint_returns_data_with_mocked_api(
        self, db: Session, sample_ultrahuman_api_response: dict
    ) -> None:
        """Verify timeseries endpoint returns activity samples after sync."""
        user = UserFactory()
        UserConnectionFactory(user=user, provider="ultrahuman", status="active", access_token="test_token")
        DataSourceFactory(user_id=user.id, provider="ultrahuman")

        factory = ProviderFactory()
        strategy = factory.get_provider("ultrahuman")
        provider_impl = strategy.data_247
        assert isinstance(provider_impl, Ultrahuman247Data)

        end_time = datetime(2024, 1, 16, tzinfo=timezone.utc)
        start_time = datetime(2024, 1, 15, tzinfo=timezone.utc)

        with patch.object(provider_impl, "_make_api_request", return_value=sample_ultrahuman_api_response):
            results = provider_impl.load_and_save_all(db, user.id, start_time=start_time, end_time=end_time)
            db.commit()

            assert results.outcomes["activity_samples"].rows_written > 0, (
                "Fixture has activity data, expected synced samples"
            )
            samples = db.query(SeriesSample).join(DataSource).filter(DataSource.user_id == user.id).all()

            assert len(samples) > 0, "Should have activity samples after sync"


class TestUltrahumanErrorHandling:
    """Tests for Ultrahuman error handling and edge cases."""

    def test_sync_handles_no_data_days_with_mocked_api(self, db: Session) -> None:
        """Verify sync continues when API returns no data for some days."""
        user = UserFactory()
        UserConnectionFactory(user=user, provider="ultrahuman", status="active", access_token="test_token")
        DataSourceFactory(user_id=user.id, provider="ultrahuman")

        factory = ProviderFactory()
        strategy = factory.get_provider("ultrahuman")
        provider_impl = strategy.data_247
        assert isinstance(provider_impl, Ultrahuman247Data)

        empty_response = {"data": {"metric_data": []}}

        end_time = datetime(2024, 1, 16, tzinfo=timezone.utc)
        start_time = datetime(2024, 1, 14, tzinfo=timezone.utc)

        with patch.object(provider_impl, "_make_api_request", return_value=empty_response):
            results = provider_impl.load_and_save_all(db, user.id, start_time=start_time, end_time=end_time)
            db.commit()

            assert results is not None
            assert "sleep" in results.outcomes
            assert "activity_samples" in results.outcomes
            assert isinstance(results.outcomes["sleep"].rows_written, int)
            assert isinstance(results.outcomes["activity_samples"].rows_written, int)

    def test_sync_handles_partial_data_with_mocked_api(self, db: Session) -> None:
        """Verify sync handles days with partial data (sleep but no activity)."""
        user = UserFactory()
        UserConnectionFactory(user=user, provider="ultrahuman", status="active", access_token="test_token")
        DataSourceFactory(user_id=user.id, provider="ultrahuman")

        factory = ProviderFactory()
        strategy = factory.get_provider("ultrahuman")
        provider_impl = strategy.data_247
        assert isinstance(provider_impl, Ultrahuman247Data)

        partial_response = {
            "data": {
                "metric_data": [
                    {
                        "type": "Sleep",
                        "object": {
                            "date": "2024-01-15",
                            "bedtime_start": 1705309200,
                            "bedtime_end": 1705334400,
                            "quick_metrics": [
                                {"type": "time_in_bed", "value": 25200},
                                {"type": "sleep_efic", "value": 90.5},
                            ],
                            "sleep_stages": [
                                {"type": "deep_sleep", "stage_time": 3600},
                                {"type": "light_sleep", "stage_time": 16200},
                                {"type": "rem_sleep", "stage_time": 3600},
                                {"type": "awake", "stage_time": 1800},
                            ],
                        },
                    },
                ]
            }
        }

        end_time = datetime(2024, 1, 16, tzinfo=timezone.utc)
        start_time = datetime(2024, 1, 14, tzinfo=timezone.utc)

        with patch.object(provider_impl, "_make_api_request", return_value=partial_response):
            results = provider_impl.load_and_save_all(db, user.id, start_time=start_time, end_time=end_time)
            db.commit()

            assert results is not None
            assert "sleep" in results.outcomes
            assert "activity_samples" in results.outcomes
            assert isinstance(results.outcomes["sleep"].rows_written, int)
            assert isinstance(results.outcomes["activity_samples"].rows_written, int)

    def test_sync_respects_date_range_with_mocked_api(self, db: Session, sample_ultrahuman_api_response: dict) -> None:
        """Verify sync only fetches data within specified date range."""
        user = UserFactory()
        UserConnectionFactory(user=user, provider="ultrahuman", status="active", access_token="test_token")
        DataSourceFactory(user_id=user.id, provider="ultrahuman")

        factory = ProviderFactory()
        strategy = factory.get_provider("ultrahuman")
        provider_impl = strategy.data_247
        assert isinstance(provider_impl, Ultrahuman247Data)

        end_time = datetime(2024, 1, 17, tzinfo=timezone.utc)
        start_time = datetime(2024, 1, 15, tzinfo=timezone.utc)

        with patch.object(provider_impl, "_make_api_request", return_value=sample_ultrahuman_api_response):
            results = provider_impl.load_and_save_all(db, user.id, start_time=start_time, end_time=end_time)
            db.commit()

            assert results.outcomes["sleep"].rows_written > 0, "Fixture has sleep data, expected synced sessions"
            records = (
                db.query(EventRecord)
                .join(DataSource)
                .filter(
                    DataSource.user_id == user.id,
                    EventRecord.category == "sleep",
                )
                .all()
            )

            for record in records:
                assert record.start_datetime >= start_time, (
                    f"Sleep record {record.start_datetime} is before start time {start_time}"
                )
                assert record.start_datetime <= end_time, (
                    f"Sleep record {record.start_datetime} is after end time {end_time}"
                )


class TestUltrahumanSyncCountContract:
    """Guards the fix for the "Success but 0 items" sync report.

    The sync orchestrator takes its headline count from ``Sync247Result.rows_written``
    and its new-vs-updated split from ``.inserted`` / ``.updated``, so every
    write path has to report the rows it actually persisted.
    """

    def test_saved_counts_are_countable_by_the_orchestrator(
        self, db: Session, sample_ultrahuman_api_response: dict
    ) -> None:
        user = UserFactory()
        UserConnectionFactory(user=user, provider="ultrahuman", status="active", access_token="test_token")
        DataSourceFactory(user_id=user.id, provider="ultrahuman")

        factory = ProviderFactory()
        strategy = factory.get_provider("ultrahuman")
        provider_impl = strategy.data_247
        assert isinstance(provider_impl, Ultrahuman247Data)

        # Single-day range: the mock returns the same payload for every day in
        # the range, so a multi-day range would upsert-update the same rows and
        # produce updates. One day keeps the first sync deterministically all
        # inserts.
        start_time = datetime(2024, 1, 15, tzinfo=timezone.utc)
        end_time = datetime(2024, 1, 15, tzinfo=timezone.utc)

        with patch.object(provider_impl, "_make_api_request", return_value=sample_ultrahuman_api_response):
            results = provider_impl.load_and_save_all(db, user.id, start_time=start_time, end_time=end_time)
            db.commit()

        # Fixture carries sleep, daily scores and activity data.
        assert results.outcomes["sleep"].rows_written > 0
        assert results.outcomes["recovery"].rows_written == 3
        assert results.outcomes["activity_samples"].rows_written > 0

        # Daily scores and activity samples go through bulk upserts, so they carry the split;
        # sleep sessions are merged one at a time and report a total only.
        assert results.outcomes["activity_samples"].counts is not None
        assert results.outcomes["activity_samples"].counts.updated == 0
        assert results.outcomes["recovery"].counts is not None
        assert results.outcomes["recovery"].counts.updated == 0
        assert results.outcomes["sleep"].counts is None

        # The orchestrator's headline count covers all of them, split or not.
        assert results.rows_written == sum(
            results.outcomes[t].rows_written for t in ("sleep", "recovery", "activity_samples")
        )
        assert results.inserted == (
            results.outcomes["recovery"].rows_written + results.outcomes["activity_samples"].rows_written
        )
        assert results.updated == 0
        assert not results.failures

    def test_a_null_metric_object_does_not_drop_its_siblings(
        self, db: Session, sample_ultrahuman_api_response: dict
    ) -> None:
        user = UserFactory()
        UserConnectionFactory(user=user, provider="ultrahuman", status="active", access_token="test_token")
        DataSourceFactory(user_id=user.id, provider="ultrahuman")
        provider_impl = ProviderFactory().get_provider("ultrahuman").data_247
        assert isinstance(provider_impl, Ultrahuman247Data)

        response = {
            "data": {
                "metric_data": [
                    *sample_ultrahuman_api_response["data"]["metric_data"],
                    {"type": "temp", "object": None},
                ]
            }
        }
        day = datetime(2024, 1, 15, tzinfo=timezone.utc)

        with patch.object(provider_impl, "_make_api_request", return_value=response):
            results = provider_impl.load_and_save_all(db, user.id, start_time=day, end_time=day)

        assert not results.failures
        assert results.outcomes["activity_samples"].rows_written > 0

    def test_resync_upserts_instead_of_duplicating(self, db: Session, sample_ultrahuman_api_response: dict) -> None:
        """Re-syncing the same day must upsert (dedupe), not insert duplicate rows."""
        user = UserFactory()
        UserConnectionFactory(user=user, provider="ultrahuman", status="active", access_token="test_token")
        DataSourceFactory(user_id=user.id, provider="ultrahuman")

        factory = ProviderFactory()
        strategy = factory.get_provider("ultrahuman")
        provider_impl = strategy.data_247
        assert isinstance(provider_impl, Ultrahuman247Data)

        # Single-day range so the first sync is deterministically all inserts;
        # the re-sync then exercises the upsert/update path on the same rows.
        start_time = datetime(2024, 1, 15, tzinfo=timezone.utc)
        end_time = datetime(2024, 1, 15, tzinfo=timezone.utc)

        def _sample_rows() -> int:
            return (
                db.query(SeriesSample)
                .join(DataSource)
                .filter(DataSource.user_id == user.id, DataSource.provider == "ultrahuman")
                .count()
            )

        with patch.object(provider_impl, "_make_api_request", return_value=sample_ultrahuman_api_response):
            first = provider_impl.load_and_save_all(db, user.id, start_time=start_time, end_time=end_time)
            db.commit()
            rows_after_first = _sample_rows()

            second = provider_impl.load_and_save_all(db, user.id, start_time=start_time, end_time=end_time)
            db.commit()
            rows_after_second = _sample_rows()

        # First sync inserts; second sync updates the same rows in place.
        assert first.inserted > 0
        assert first.updated == 0
        assert second.inserted == 0
        assert second.updated == first.inserted
        assert rows_after_second == rows_after_first, "Re-sync must not duplicate timeseries rows"


class TestUltrahumanScoresIntegration:
    """Ultrahuman's scores reach the health scores and recovery summary endpoints like WHOOP's do."""

    DAY = datetime(2024, 1, 15, tzinfo=timezone.utc)

    def _sync(self, db: Session, response: dict) -> UUID:
        user = UserFactory()
        UserConnectionFactory(user=user, provider="ultrahuman", status="active", access_token="test_token")
        provider_impl = ProviderFactory().get_provider("ultrahuman").data_247
        assert isinstance(provider_impl, Ultrahuman247Data)
        with patch.object(provider_impl, "_make_api_request", return_value=response):
            results = provider_impl.load_and_save_all(db, user.id, start_time=self.DAY, end_time=self.DAY)
        assert not results.failures
        return user.id

    @staticmethod
    def _scores(client: TestClient, user_id: UUID) -> dict[str, dict[str, Any]]:
        response = client.get(
            f"/api/v1/users/{user_id}/health-scores",
            params={"provider": "ultrahuman"},
            headers=api_key_headers(ApiKeyFactory().plain_key),
        )
        assert response.status_code == 200, response.text
        scores = response.json()["data"]
        by_category = {score["category"]: score for score in scores}
        assert len(by_category) == len(scores), "one score per category for the day"
        return by_category

    @staticmethod
    def _values(score: dict[str, Any]) -> dict[str, Any]:
        return {name: (c["value"], c["qualifier"]) for name, c in score["components"].items()}

    def test_daily_scores_carry_the_metrics_sent_with_them(
        self, client: TestClient, db: Session, sample_ultrahuman_api_response: dict
    ) -> None:
        scores = self._scores(client, self._sync(db, sample_ultrahuman_api_response))

        assert {c: scores[c]["value"] for c in ("recovery", "movement_index", "metabolic")} == {
            "recovery": 78,
            "movement_index": 65,
            "metabolic": 74,
        }
        for category in ("recovery", "movement_index", "metabolic"):
            assert scores[category]["recorded_at"].startswith("2024-01-15T00:00:00")
            assert scores[category]["event_record_id"] is None
        assert self._values(scores["recovery"]) == {
            "resting_heart_rate": (51, None),
            "hrv_rmssd_milli": (48, None),
            "spo2_percentage": (97, None),
            "skin_temp_deviation_celsius": (-0.2, None),
        }
        assert self._values(scores["movement_index"]) == {"movements": (3200, None)}
        assert self._values(scores["metabolic"]) == {
            "average_glucose_mg_dl": (101, None),
            "glucose_variability_percent": (14.5, None),
            "hba1c_percent": (5.3, None),
            "time_in_target_percent": (88, None),
        }

    def test_sleep_score_sits_at_the_night_end_and_links_its_session(
        self, client: TestClient, db: Session, sample_ultrahuman_api_response: dict
    ) -> None:
        user_id = self._sync(db, sample_ultrahuman_api_response)
        sleep = self._scores(client, user_id)["sleep"]

        sessions = client.get(
            f"/api/v1/users/{user_id}/events/sleep",
            params={"start_date": "2024-01-14T00:00:00Z", "end_date": "2024-01-16T00:00:00Z"},
            headers=api_key_headers(ApiKeyFactory().plain_key),
        ).json()["data"]
        assert [s["id"] for s in sessions] == [sleep["event_record_id"]]
        assert sleep["value"] == 81
        assert sleep["recorded_at"].startswith("2024-01-15T10:20:00")
        assert self._values(sleep) == {
            "sleep_efficiency": (88, "Optimal"),
            "temperature": (92, "Optimal"),
            "spo2_percentage": (97, None),
            "toss_and_turn": (12, None),
            "morning_alertness": (7, None),
            "avg_heart_rate": (52, None),
            "avg_hrv": (48, None),
            "sleep_cycles": (3, None),
        }

    def test_a_score_without_a_value_is_not_written(
        self, client: TestClient, db: Session, sample_ultrahuman_api_response: dict
    ) -> None:
        response = copy.deepcopy(sample_ultrahuman_api_response)
        for item in response["data"]["metric_data"]:
            if item["type"] == "metabolic_score":
                item["object"]["value"] = None

        scores = self._scores(client, self._sync(db, response))

        assert set(scores) == {"recovery", "movement_index", "sleep"}

    def test_a_resent_day_replaces_its_scores(
        self, client: TestClient, db: Session, sample_ultrahuman_api_response: dict
    ) -> None:
        user_id = self._sync(db, sample_ultrahuman_api_response)
        later = copy.deepcopy(sample_ultrahuman_api_response)
        for item in later["data"]["metric_data"]:
            if item["type"] == "movement_index":
                item["object"]["value"] = 81
        provider_impl = ProviderFactory().get_provider("ultrahuman").data_247
        with patch.object(provider_impl, "_make_api_request", return_value=later):
            provider_impl.load_and_save_all(db, user_id, start_time=self.DAY, end_time=self.DAY)

        assert self._scores(client, user_id)["movement_index"]["value"] == 81

    def test_recovery_summary_reads_the_score_and_its_vitals(
        self, client: TestClient, db: Session, sample_ultrahuman_api_response: dict
    ) -> None:
        user_id = self._sync(db, sample_ultrahuman_api_response)

        response = client.get(
            f"/api/v1/users/{user_id}/summaries/recovery",
            params={"start_date": "2024-01-15T00:00:00Z", "end_date": "2024-01-16T00:00:00Z"},
            headers=api_key_headers(ApiKeyFactory().plain_key),
        )

        assert response.status_code == 200, response.text
        [day] = response.json()["data"]
        assert day["date"] == "2024-01-15"
        assert day["source"]["provider"] == "ultrahuman"
        assert (
            day["recovery_score"],
            day["resting_heart_rate_bpm"],
            day["avg_hrv_rmssd_ms"],
            day["avg_spo2_percent"],
        ) == (78, 51, 48, 97)

    def test_glucose_and_inactive_time_reach_the_timeseries_endpoint(
        self, client: TestClient, db: Session, sample_ultrahuman_api_response: dict
    ) -> None:
        user_id = self._sync(db, sample_ultrahuman_api_response)

        def read(series_type: str, resolution: str) -> list[tuple[Any, str]]:
            response = client.get(
                f"/api/v1/users/{user_id}/timeseries",
                params={
                    "start_time": "2024-01-15T00:00:00Z",
                    "end_time": "2024-01-16T00:00:00Z",
                    "types": series_type,
                    "resolution": resolution,
                },
                headers=api_key_headers(ApiKeyFactory().plain_key),
            )
            assert response.status_code == 200, response.text
            return [(s["value"], s["unit"]) for s in response.json()["data"]]

        assert read("blood_glucose", "raw") == [(92, "mg_dl"), (118, "mg_dl")]
        assert read("sedentary_time", "1day") == [(210, "minutes")]
