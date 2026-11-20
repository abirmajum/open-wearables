"""
Tests for Ultrahuman 24/7 data implementation.

Tests the Ultrahuman247Data class for sleep, recovery, and activity data handling.
"""

from collections.abc import Iterator
from datetime import date, datetime, timezone
from typing import Any
from unittest.mock import MagicMock, patch
from uuid import uuid4

import httpx
import pytest
from sqlalchemy.orm import Session

from app.models import User
from app.repositories.user_connection_repository import UserConnectionRepository
from app.repositories.user_repository import UserRepository
from app.schemas.sync_status import SyncRunContext, SyncScope, SyncSource, SyncStatus
from app.services.providers.sync_247_result import Sync247Result
from app.services.providers.ultrahuman.data_247 import Ultrahuman247Data, _profile_attempt_allowed
from app.services.providers.ultrahuman.oauth import UltrahumanOAuth
from app.utils.context import sync_run_context
from app.utils.exceptions import (
    ProviderAuthRejectedError,
    ProviderError,
    ProviderNotConnectedError,
    ProviderRateLimitError,
    ReauthRequiredError,
)
from tests.factories import UserFactory

MODULE = "app.services.providers.ultrahuman.data_247"


class TestUltrahuman247Data:
    """Test suite for Ultrahuman247Data."""

    def test_ultrahuman_247_initialization(self, db: Session) -> None:
        """Should initialize Ultrahuman247Data successfully."""
        # Arrange
        user_repo = UserRepository(User)
        connection_repo = UserConnectionRepository()
        oauth = UltrahumanOAuth(
            user_repo=user_repo,
            connection_repo=connection_repo,
            provider_name="ultrahuman",
            api_base_url="https://partner.ultrahuman.com",
        )

        # Act
        data_247 = Ultrahuman247Data(
            provider_name="ultrahuman",
            api_base_url="https://partner.ultrahuman.com",
            oauth=oauth,
        )

        # Assert
        assert data_247.provider_name == "ultrahuman"
        assert data_247.api_base_url == "https://partner.ultrahuman.com"
        assert data_247.oauth is not None


class TestUltrahumanSleepData:
    """Tests for Ultrahuman sleep data handling."""

    def test_normalize_sleep_with_complete_data(self, db: Session) -> None:
        """Test normalizing sleep data with all fields present."""
        # Arrange
        from app.models import User
        from app.repositories.user_connection_repository import UserConnectionRepository
        from app.repositories.user_repository import UserRepository

        user = UserFactory()
        user_repo = UserRepository(User)
        connection_repo = UserConnectionRepository()
        oauth = UltrahumanOAuth(
            user_repo=user_repo,
            connection_repo=connection_repo,
            provider_name="ultrahuman",
            api_base_url="https://partner.ultrahuman.com",
        )

        data_247 = Ultrahuman247Data(
            provider_name="ultrahuman",
            api_base_url="https://partner.ultrahuman.com",
            oauth=oauth,
        )

        raw_sleep = {
            "ultrahuman_date": "2025-01-14",
            "bedtime_start": 1736816400,  # 2025-01-14T01:00:00Z
            "bedtime_end": 1736928000,  # 2025-01-15T08:00:00Z
            "quick_metrics": [
                {"type": "time_in_bed", "value": 25200},
                {"type": "sleep_efic", "value": 90},
            ],
            "sleep_stages": [
                {"type": "deep_sleep", "stage_time": 3600},
                {"type": "rem_sleep", "stage_time": 5400},
                {"type": "light_sleep", "stage_time": 16200},
                {"type": "awake", "stage_time": 1800},
            ],
        }

        # Act
        normalized = data_247.normalize_sleep(raw_sleep, user.id)

        # Assert
        assert normalized["user_id"] == user.id
        assert normalized["provider"] == "ultrahuman"
        assert normalized["ultrahuman_date"] == "2025-01-14"
        assert normalized["start_time"].year == 2025
        assert normalized["end_time"].year == 2025
        assert normalized["duration_seconds"] == 25200
        assert normalized["efficiency_percent"] == 90
        assert normalized["stages"]["deep_seconds"] == 3600
        assert normalized["stages"]["rem_seconds"] == 5400
        assert normalized["stages"]["light_seconds"] == 16200

    def test_normalize_sleep_with_minimal_data(self, db: Session) -> None:
        """Test normalizing sleep data with minimal fields."""
        # Arrange
        from app.models import User
        from app.repositories.user_connection_repository import UserConnectionRepository
        from app.repositories.user_repository import UserRepository

        user = UserFactory()
        user_repo = UserRepository(User)
        connection_repo = UserConnectionRepository()
        oauth = UltrahumanOAuth(
            user_repo=user_repo,
            connection_repo=connection_repo,
            provider_name="ultrahuman",
            api_base_url="https://partner.ultrahuman.com",
        )

        data_247 = Ultrahuman247Data(
            provider_name="ultrahuman",
            api_base_url="https://partner.ultrahuman.com",
            oauth=oauth,
        )

        raw_sleep = {
            "ultrahuman_date": "2025-01-14",
        }

        # Act
        normalized = data_247.normalize_sleep(raw_sleep, user.id)

        # Assert
        assert normalized["user_id"] == user.id
        assert normalized["provider"] == "ultrahuman"
        assert normalized["ultrahuman_date"] == "2025-01-14"
        assert normalized["duration_seconds"] == 0
        assert normalized["efficiency_percent"] is None

    @staticmethod
    def _data_247() -> Ultrahuman247Data:
        return Ultrahuman247Data(
            provider_name="ultrahuman", api_base_url="https://partner.ultrahuman.com", oauth=MagicMock()
        )

    _NIGHT = {
        "ultrahuman_date": "2025-01-14",
        "bedtime_start": 1736816400,
        "bedtime_end": 1736841600,
        "quick_metrics": [{"type": "time_in_bed", "value": 25200}],
    }

    @patch("app.services.providers.ultrahuman.data_247.event_record_service")
    def test_save_sleep_keeps_unreported_stages_none(self, mock_event: MagicMock, db: Session) -> None:
        data_247 = self._data_247()
        raw_sleep = {**self._NIGHT, "sleep_stages": [{"type": "awake", "stage_time": 1800}]}

        normalized = data_247.normalize_sleep(raw_sleep, UserFactory().id)
        data_247.save_sleep_data(db, normalized["user_id"], normalized)

        detail = mock_event.create_or_merge_sleep.call_args[0][3]
        assert detail.sleep_deep_minutes is None
        assert detail.sleep_light_minutes is None
        assert detail.sleep_rem_minutes is None
        assert detail.sleep_awake_minutes == 30
        # Total sleep falls back to time in bed minus awake time
        assert detail.sleep_total_duration_minutes == 390

    @patch("app.services.providers.ultrahuman.data_247.event_record_service")
    def test_save_sleep_keeps_reported_zero_stages(self, mock_event: MagicMock, db: Session) -> None:
        data_247 = self._data_247()
        raw_sleep = {
            **self._NIGHT,
            "sleep_stages": [
                {"type": "deep_sleep", "stage_time": 3600},
                {"type": "rem_sleep", "stage_time": 0},
                {"type": "light_sleep", "stage_time": 16200},
                {"type": "awake", "stage_time": 0},
            ],
        }

        normalized = data_247.normalize_sleep(raw_sleep, UserFactory().id)
        data_247.save_sleep_data(db, normalized["user_id"], normalized)

        detail = mock_event.create_or_merge_sleep.call_args[0][3]
        assert detail.sleep_rem_minutes == 0
        assert detail.sleep_awake_minutes == 0
        assert detail.sleep_total_duration_minutes == 330

    @patch("app.services.providers.ultrahuman.data_247.event_record_service")
    def test_save_sleep_infers_total_when_phases_are_zero(self, mock_event: MagicMock, db: Session) -> None:
        data_247 = self._data_247()
        raw_sleep = {
            **self._NIGHT,
            "sleep_stages": [
                {"type": "deep_sleep", "stage_time": 0},
                {"type": "rem_sleep", "stage_time": 0},
                {"type": "light_sleep", "stage_time": 0},
                {"type": "awake", "stage_time": 1800},
            ],
        }

        normalized = data_247.normalize_sleep(raw_sleep, UserFactory().id)
        data_247.save_sleep_data(db, normalized["user_id"], normalized)

        detail = mock_event.create_or_merge_sleep.call_args[0][3]
        assert detail.sleep_deep_minutes == 0
        assert detail.sleep_total_duration_minutes == 390

    @patch("app.services.providers.ultrahuman.data_247.event_record_service")
    def test_save_sleep_without_stages_takes_time_in_bed_as_total(self, mock_event: MagicMock, db: Session) -> None:
        data_247 = self._data_247()

        normalized = data_247.normalize_sleep(dict(self._NIGHT), UserFactory().id)
        data_247.save_sleep_data(db, normalized["user_id"], normalized)

        detail = mock_event.create_or_merge_sleep.call_args[0][3]
        assert detail.sleep_deep_minutes is None
        assert detail.sleep_awake_minutes is None
        assert detail.sleep_total_duration_minutes == 420

    def test_normalize_sleep_parses_sleep_graph_stages(self, db: Session) -> None:
        """sleep_graph.data intervals are parsed into canonical SleepStage objects."""
        from app.constants.sleep import SleepStageType

        user = UserFactory()
        oauth = UltrahumanOAuth(
            user_repo=UserRepository(User),
            connection_repo=UserConnectionRepository(),
            provider_name="ultrahuman",
            api_base_url="https://partner.ultrahuman.com",
        )
        data_247 = Ultrahuman247Data(
            provider_name="ultrahuman",
            api_base_url="https://partner.ultrahuman.com",
            oauth=oauth,
        )

        raw_sleep = {
            "ultrahuman_date": "2025-01-14",
            "bedtime_start": 1736816400,
            "bedtime_end": 1736820000,
            "sleep_graph": {
                "data": [
                    # deliberately out of order to verify sorting
                    {"start": 1736817000, "end": 1736817600, "type": "deep_sleep"},
                    {"start": 1736816400, "end": 1736817000, "type": "awake"},
                    {"start": 1736817600, "end": 1736818200, "type": "rem_sleep"},
                    {"start": 1736818200, "end": 1736818800, "type": "somethingelse"},  # unknown → UNKNOWN
                    {"start": 1736818800, "type": "light_sleep"},  # missing end → skipped
                ],
            },
        }

        normalized = data_247.normalize_sleep(raw_sleep, user.id)
        stages = normalized["stage_timestamps"]

        # 4 valid intervals (the missing-end one is skipped), sorted by start_time
        assert [s.stage for s in stages] == [
            SleepStageType.AWAKE,
            SleepStageType.DEEP,
            SleepStageType.REM,
            SleepStageType.UNKNOWN,
        ]
        assert stages[0].start_time.tzinfo is not None
        assert stages[0].start_time < stages[1].start_time
        assert stages[1].start_time == stages[0].end_time

    def test_normalize_sleep_without_sleep_graph_yields_no_stages(self, db: Session) -> None:
        """A payload with no sleep_graph produces an empty stage list, not an error."""
        user = UserFactory()
        oauth = UltrahumanOAuth(
            user_repo=UserRepository(User),
            connection_repo=UserConnectionRepository(),
            provider_name="ultrahuman",
            api_base_url="https://partner.ultrahuman.com",
        )
        data_247 = Ultrahuman247Data(
            provider_name="ultrahuman",
            api_base_url="https://partner.ultrahuman.com",
            oauth=oauth,
        )

        normalized = data_247.normalize_sleep({"ultrahuman_date": "2025-01-14"}, user.id)

        assert normalized["stage_timestamps"] == []


class TestUltrahumanActivitySamples:
    """Tests for Ultrahuman activity samples handling."""

    def test_normalize_activity_samples(self, db: Session) -> None:
        """Test normalizing activity samples."""
        # Arrange
        from app.models import User
        from app.repositories.user_connection_repository import UserConnectionRepository
        from app.repositories.user_repository import UserRepository

        user = UserFactory()
        user_repo = UserRepository(User)
        connection_repo = UserConnectionRepository()
        oauth = UltrahumanOAuth(
            user_repo=user_repo,
            connection_repo=connection_repo,
            provider_name="ultrahuman",
            api_base_url="https://partner.ultrahuman.com",
        )

        data_247 = Ultrahuman247Data(
            provider_name="ultrahuman",
            api_base_url="https://partner.ultrahuman.com",
            oauth=oauth,
        )

        raw_samples = [
            {
                "type": "hr",
                "values": [
                    {"timestamp": 1705309200, "value": 72},
                    {"timestamp": 1705309500, "value": 75},
                ],
            },
            {
                "type": "hrv",
                "values": [
                    {"timestamp": 1705309200, "value": 45},
                    {"timestamp": 1705309500, "value": 50},
                ],
            },
            {
                "type": "temp",
                "values": [
                    {"timestamp": 1705309200, "value": 36.5},
                    {"timestamp": 1705309500, "value": 36.6},
                ],
            },
            {
                "type": "steps",
                "values": [
                    {"timestamp": 1705309200, "value": 8500},
                ],
            },
        ]

        # Act
        normalized = data_247.normalize_activity_samples(raw_samples, user.id)

        # Assert
        assert {series: [r["value"] for r in readings] for series, readings in normalized.items()} == {
            "heart_rate": [72, 75],
            "heart_rate_variability_rmssd": [45, 50],
            "skin_temperature": [36.5, 36.6],
            "steps": [8500],
        }


class TestUltrahumanFetchFailures:
    """Which failed fetches end the run, fail a day, or read as a day without data."""

    WINDOW = {
        "start_time": datetime(2026, 1, 1, tzinfo=timezone.utc),
        "end_time": datetime(2026, 1, 2, tzinfo=timezone.utc),
    }
    EMPTY_DAY = {"data": {"metric_data": []}, "error": None, "status": 200}

    @pytest.fixture
    def data_247(self) -> Iterator[Ultrahuman247Data]:
        data_247 = Ultrahuman247Data(
            provider_name="ultrahuman", api_base_url="https://partner.ultrahuman.com", oauth=MagicMock()
        )
        known = MagicMock(provider_user_id="uh-1")  # the account id is known: no profile lookup
        with (
            patch.object(data_247, "_save_daily_activity", return_value=0),
            patch.object(data_247.connection_repo, "get_by_user_and_provider", return_value=known),
            # The window's days are young enough to be retried.
            patch(f"{MODULE}._utc_today", return_value=date(2026, 1, 2)),
        ):
            yield data_247

    def _run(self, data_247: Ultrahuman247Data, *answers: Any) -> tuple[Sync247Result, MagicMock, MagicMock]:
        with (
            patch(f"{MODULE}.logger") as mock_logger,
            patch.object(data_247, "_make_api_request", side_effect=answers) as mock_request,
        ):
            result = data_247.load_and_save_all(MagicMock(), uuid4(), **self.WINDOW)
        return result, mock_request, mock_logger

    @pytest.mark.parametrize(
        "error",
        [
            ProviderAuthRejectedError("ultrahuman"),
            ProviderNotConnectedError("ultrahuman"),
            ReauthRequiredError("ultrahuman", uuid4(), "rejected for a day"),
            ProviderError("ultrahuman", upstream_status=403),
        ],
    )
    def test_unusable_access_ends_the_run(self, data_247: Ultrahuman247Data, error: Exception) -> None:
        with (
            patch.object(data_247, "_make_api_request", side_effect=error) as mock_request,
            pytest.raises(type(error)),
        ):
            data_247.load_and_save_all(MagicMock(), uuid4(), **self.WINDOW)

        # every remaining day would fail the same way, so the first one stops it
        mock_request.assert_called_once()

    @pytest.mark.parametrize(
        ("error", "error_code", "alert"),
        [
            (ProviderError("ultrahuman", upstream_status=500), "http_500", True),  # an outage is reported
            (ProviderError("ultrahuman"), "provider_error", False),  # no response: not on every pull
        ],
    )
    def test_a_failed_day_fails_the_run_and_the_walk_goes_on(
        self, data_247: Ultrahuman247Data, error: Exception, error_code: str, alert: bool
    ) -> None:
        """A failed day is never read as a day without data: the run is not a success, so the cursor stays."""
        result, mock_request, mock_logger = self._run(data_247, error, error)

        assert mock_request.call_count == 2
        assert result.outcomes["daily_metrics"].error_codes == (error_code, error_code)
        assert result.summary().status is SyncStatus.FAILED
        # one line per day, the date on it
        warnings = mock_logger.warning.call_args_list
        assert [w.kwargs["date"] for w in warnings] == ["2026-01-01", "2026-01-02"]
        assert {w.kwargs["alert"] for w in warnings} == {alert}

    def test_one_failed_day_makes_the_run_partial(self, data_247: Ultrahuman247Data) -> None:
        with patch.object(data_247, "_save_daily_activity", return_value=3):
            result, _, _ = self._run(data_247, ProviderError("ultrahuman", upstream_status=502), self.EMPTY_DAY)

        assert result.failures == {"daily_metrics": "http_502"}
        assert result.summary().status is SyncStatus.PARTIAL

    def test_a_throttle_that_outlasts_the_backoff_stops_the_walk(self, data_247: Ultrahuman247Data) -> None:
        result, mock_request, mock_logger = self._run(
            data_247, ProviderRateLimitError("ultrahuman", upstream_status=429)
        )

        mock_request.assert_called_once()
        assert result.failures == {"daily_metrics": "http_429"}
        assert result.summary().status is SyncStatus.FAILED
        assert mock_logger.warning.call_args.kwargs["alert"] is False

    def test_a_day_without_data_is_empty_not_failed(self, data_247: Ultrahuman247Data) -> None:
        result, _, _ = self._run(data_247, self.EMPTY_DAY, self.EMPTY_DAY)

        assert not result.failures
        assert "daily_metrics" not in result.outcomes
        assert result.summary().status is SyncStatus.SKIPPED

    @pytest.mark.parametrize(
        "not_found",
        [
            ProviderError("ultrahuman", upstream_status=404),
            {"data": None, "error": "no data for this date", "status": 404},
            {"data": None, "error": "no data for this date", "status": "404"},
        ],
    )
    def test_a_404_for_a_day_is_an_empty_day(self, data_247: Ultrahuman247Data, not_found: Any) -> None:
        """Undocumented for a day: a 404, in the status or in the envelope, must not hold the live cursor forever."""
        result, mock_request, mock_logger = self._run(data_247, not_found, self.EMPTY_DAY)

        assert mock_request.call_count == 2
        assert not result.failures
        assert result.summary().status is SyncStatus.SKIPPED
        assert mock_logger.info.call_args_list[0].kwargs["date"] == "2026-01-01"
        mock_logger.warning.assert_not_called()

    def test_an_outage_in_the_envelope_fails_the_day(self, data_247: Ultrahuman247Data) -> None:
        result, _, _ = self._run(data_247, {"data": None, "error": "internal error", "status": 503}, self.EMPTY_DAY)

        assert result.failures == {"daily_metrics": "http_503"}
        assert result.summary().status is SyncStatus.FAILED

    @pytest.mark.parametrize(
        "unusable",
        [
            ProviderError("ultrahuman", upstream_status=400),
            ProviderError("ultrahuman", upstream_status=422),
            ProviderError("ultrahuman", upstream_status=200),  # a 200 that is not JSON
            {"data": None, "error": "invalid date", "status": 400},
            {"data": None, "error": "something went wrong"},  # an error without a status
            {"data": None, "error": None, "status": 401},
        ],
    )
    def test_an_answer_that_fails_every_time_skips_the_day(self, data_247: Ultrahuman247Data, unusable: Any) -> None:
        """Retrying would fail the same way every run and stall the cursor: the day is skipped and reported."""
        result, mock_request, mock_logger = self._run(data_247, unusable, self.EMPTY_DAY)

        assert mock_request.call_count == 2
        assert not result.failures
        assert result.outcomes["daily_metrics"].skipped == 1
        assert result.summary().status is SyncStatus.SKIPPED
        [warning] = mock_logger.warning.call_args_list
        assert (warning.kwargs["date"], warning.kwargs["alert"]) == ("2026-01-01", True)

    def test_an_answer_our_code_cannot_read_skips_the_day(self, data_247: Ultrahuman247Data) -> None:
        with patch.object(data_247, "_items_by_type", side_effect=[ValueError("unexpected shape"), {}]):
            result, _, mock_logger = self._run(data_247, self.EMPTY_DAY, self.EMPTY_DAY)

        assert not result.failures
        assert result.outcomes["daily_metrics"].skipped == 1
        assert mock_logger.warning.call_args.kwargs["alert"] is True

    def test_an_absurd_day_start_does_not_fail_the_day(self, data_247: Ultrahuman247Data) -> None:
        day = {
            "data": {
                "metric_data": [
                    {"type": "active_minutes", "object": {"value": 42, "day_start_timestamp": 1e300}},
                    {"type": "recovery_index", "object": {"value": 80, "day_start_timestamp": -1e300}},
                ]
            }
        }
        # The real save: nothing in the day is a representable instant, so nothing reaches the database.
        with patch.object(data_247, "_save_daily_activity", Ultrahuman247Data._save_daily_activity.__get__(data_247)):
            result, _, _ = self._run(data_247, day, self.EMPTY_DAY)

        assert not result.failures
        assert "daily_metrics" not in result.outcomes

    def test_a_day_failing_past_the_retry_window_is_given_up(self, data_247: Ultrahuman247Data) -> None:
        with patch(f"{MODULE}._utc_today", return_value=date(2026, 1, 9)):  # Jan 1 is 8 days old, Jan 2 seven
            result, _, _ = self._run(
                data_247,
                ProviderError("ultrahuman", upstream_status=500),
                ProviderError("ultrahuman", upstream_status=500),
            )

        assert result.outcomes["daily_metrics"].error_codes == ("http_500",)  # only Jan 2 still fails
        assert result.outcomes["daily_metrics"].skipped == 1

    def test_a_backfill_keeps_reporting_an_old_failing_day(self, data_247: Ultrahuman247Data) -> None:
        historical = SyncRunContext(run_id="run-1", source=SyncSource.BACKFILL, scope=SyncScope.HISTORICAL)
        with sync_run_context(historical), patch(f"{MODULE}._utc_today", return_value=date(2026, 3, 1)):
            result, _, _ = self._run(data_247, ProviderError("ultrahuman", upstream_status=500), self.EMPTY_DAY)

        assert result.failures == {"daily_metrics": "http_500"}

    def test_unknown_metric_types_and_malformed_items_are_ignored(self, data_247: Ultrahuman247Data) -> None:
        """The workout scope adds workouts to the day's metrics, in a shape Ultrahuman does not document."""
        day = {
            "data": {
                "metric_data": [
                    "not an object",
                    {"type": ["not", "a", "name"], "object": {}},
                    {"type": "workout", "object": [{"start": 1, "end": 2}]},
                    {"type": "calories_burned", "object": {"value": 2100, "day_start_timestamp": 1767225600}},
                ]
            }
        }
        result, _, _ = self._run(data_247, day, day)

        assert not result.failures
        assert "daily_metrics" not in result.outcomes


class TestUltrahumanProviderUserId:
    """The account id a connect could not read is filled in on a later sync, without risking the connection."""

    DAY = datetime(2026, 1, 1, tzinfo=timezone.utc)
    EMPTY_DAY = {"data": {"metric_data": []}}
    PROFILE = {"user_id": "uh-42", "username": "ring@example.com"}

    @staticmethod
    def _connection(provider_user_id: str | None, scope: str | None = "ring_data profile") -> MagicMock:
        return MagicMock(provider_user_id=provider_user_id, provider_username=None, scope=scope)

    def _sync(
        self,
        connection: MagicMock,
        *,
        profile: Any = PROFILE,
        token: Any = "token",
        allowed: bool = True,
    ) -> tuple[MagicMock, MagicMock]:
        data_247 = Ultrahuman247Data(
            provider_name="ultrahuman", api_base_url="https://partner.ultrahuman.com", oauth=MagicMock()
        )
        with (
            patch.object(data_247.connection_repo, "get_by_user_and_provider", return_value=connection),
            patch.object(data_247, "_make_api_request", return_value=self.EMPTY_DAY) as mock_request,
            patch(f"{MODULE}._profile_attempt_allowed", return_value=allowed),
            patch(f"{MODULE}.get_valid_token", side_effect=[token]),
            patch(f"{MODULE}.fetch_provider_user_info", side_effect=[profile]) as mock_profile,
        ):
            data_247.load_and_save_all(MagicMock(), uuid4(), start_time=self.DAY, end_time=self.DAY)
        # The profile never goes through the authenticated path, which refreshes and revokes on a 401.
        assert [c.args[2] for c in mock_request.call_args_list] == ["/user_data/metrics"]
        return mock_request, mock_profile

    def test_a_missing_id_is_filled_in(self) -> None:
        connection = self._connection(None)

        self._sync(connection)

        assert (connection.provider_user_id, connection.provider_username) == ("uh-42", "ring@example.com")

    @pytest.mark.parametrize(
        ("profile", "token"),
        [
            (httpx.HTTPStatusError("401", request=MagicMock(), response=MagicMock(status_code=401)), "token"),
            (PROFILE, ReauthRequiredError("ultrahuman", uuid4(), "refresh token rejected")),
            (PROFILE, ProviderNotConnectedError("ultrahuman")),
        ],
    )
    def test_a_failed_lookup_does_not_fail_the_sync(self, profile: Any, token: Any) -> None:
        connection = self._connection(None)

        self._sync(connection, profile=profile, token=token)

        assert connection.provider_user_id is None

    @pytest.mark.parametrize(
        ("provider_user_id", "scope", "allowed"),
        [
            ("uh-42", "ring_data profile", True),  # known
            (None, "ring_data ring_extended_data", True),  # no profile scope
            (None, "ring_data,ring_extended_data", True),
            (None, "ring_data profile", False),  # tried within the day
        ],
    )
    def test_no_lookup(self, provider_user_id: str | None, scope: str, allowed: bool) -> None:
        _, mock_profile = self._sync(self._connection(provider_user_id, scope), allowed=allowed)

        mock_profile.assert_not_called()

    def test_a_comma_separated_scope_is_read(self) -> None:
        connection = self._connection(None, scope="ring_data,profile")

        self._sync(connection)

        assert connection.provider_user_id == "uh-42"

    def test_one_lookup_a_day_per_connection(self) -> None:
        connection_id = uuid4()

        assert _profile_attempt_allowed(connection_id) is True
        assert _profile_attempt_allowed(connection_id) is False
        assert _profile_attempt_allowed(uuid4()) is True
