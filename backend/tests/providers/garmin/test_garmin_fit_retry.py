from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest
from sqlalchemy.orm import Session

from app.config import settings
from app.integrations.celery.tasks.garmin_fit_retry_task import retry_garmin_fit_download
from app.models import EventRecord, SeriesSample, WorkoutDetails
from app.services.fit_parser import FitParseResult
from app.services.providers.garmin.data_247 import Garmin247Data, schedule_fit_retry
from app.services.providers.garmin.strategy import GarminStrategy
from app.utils.exceptions import InvalidInputError, ProviderError, ReauthRequiredError
from tests.factories import UserConnectionFactory, UserFactory
from tests.fixtures.fit_builder import make_running_fit

_MODULE = "app.services.providers.garmin.data_247"
_TASK = "app.integrations.celery.tasks.garmin_fit_retry_task"
_URL = "https://apis.garmin.com/file?id=123"
_ITEM = {"activityId": 123, "fileType": "FIT", "callbackURL": _URL}


def test_batch_download_failure_retries_and_keeps_other_files(db: Session) -> None:
    user = UserFactory()
    UserConnectionFactory(user=user, provider="garmin")
    service = GarminStrategy().data_247
    with (
        patch(f"{_MODULE}.download_binary_content", side_effect=[ProviderError("garmin"), make_running_fit()]),
        patch(f"{_MODULE}.store_fit_file"),
        patch(f"{_MODULE}.schedule_fit_retry") as schedule,
    ):
        count = service.process_items_batch(db, user.id, "activityFiles", [_ITEM, {**_ITEM, "activityId": 456}])
    schedule.assert_called_once_with(str(user.id), "123", _URL)
    assert count > 0
    assert db.query(EventRecord).filter_by(external_id="456").one()


@pytest.mark.parametrize("ingest_samples", [True, False])
def test_retry_saves_segments_and_filtered_samples(db: Session, ingest_samples: bool) -> None:
    user = UserFactory()
    UserConnectionFactory(user=user, provider="garmin")
    service = GarminStrategy().data_247
    with (
        patch(f"{_TASK}.SessionLocal") as session,
        patch(f"{_TASK}.GarminStrategy") as strategy,
        patch(f"{_MODULE}.download_binary_content", return_value=make_running_fit()),
        patch(f"{_MODULE}.store_fit_file") as store,
        patch.object(settings, "ingest_workout_samples", ingest_samples),
    ):
        session.return_value.__enter__.return_value = db
        strategy.return_value.data_247 = service
        retry_garmin_fit_download(str(user.id), "123", _URL)
    store.assert_called_once()
    db.expire_all()
    detail = db.query(WorkoutDetails).join(EventRecord).filter(EventRecord.external_id == "123").one()
    assert len(detail.segments) == 2
    assert bool(db.query(SeriesSample).count()) == ingest_samples


@pytest.mark.parametrize(
    ("error", "attempt", "expected"),
    [
        (ProviderError("garmin", upstream_status=503), 1, True),
        (ProviderError("garmin"), 1, True),
        (RuntimeError("database unavailable"), 1, True),
        (ProviderError("garmin"), 3, False),
        (ProviderError("garmin", upstream_status=400), 1, False),
        (ProviderError("garmin", upstream_status=403), 1, False),
        (ProviderError("garmin", upstream_status=410), 1, False),
        (ReauthRequiredError("garmin", uuid4(), "gone"), 1, False),
        (InvalidInputError("invalid URL"), 1, False),
    ],
)
def test_retry_failure_budget(error: Exception, attempt: int, expected: bool) -> None:
    user_id = str(uuid4())
    with (
        patch(f"{_TASK}.SessionLocal") as session,
        patch(f"{_TASK}.GarminStrategy") as strategy,
        patch(f"{_TASK}.schedule_fit_retry") as schedule,
        patch.object(settings, "garmin_fit_retry_max_attempts", 3),
    ):
        strategy.return_value.data_247.retry_fit_download.side_effect = error
        retry_garmin_fit_download(user_id, "123", _URL, attempt)
    session.return_value.__enter__.return_value.commit.assert_not_called()
    if expected:
        schedule.assert_called_once_with(user_id, "123", _URL, attempt + 1)
    else:
        schedule.assert_not_called()


@pytest.mark.parametrize("maximum", [0, 3])
def test_scheduler_backoff_and_disabled(maximum: int) -> None:
    with (
        patch(f"{_MODULE}.celery_app.send_task") as send,
        patch.object(settings, "garmin_fit_retry_max_attempts", maximum),
    ):
        scheduled = schedule_fit_retry("user", "123", _URL, attempt=2)
    assert scheduled == bool(maximum)
    if maximum:
        assert send.call_args.kwargs["countdown"] == settings.garmin_fit_retry_base_delay_seconds * 3
        assert send.call_args.kwargs["kwargs"]["attempt"] == 2
    else:
        send.assert_not_called()


def test_empty_fit_has_no_writes() -> None:
    service = Garmin247Data(provider_name="garmin", api_base_url="https://apis.garmin.com", oauth=MagicMock())

    records, details = [], []
    service.apply_fit_result(MagicMock(), uuid4(), "123", FitParseResult(), records, details)
    assert records == details == []


@pytest.mark.parametrize("status", [400, 403, 404, 410])
def test_batch_rejected_download_does_not_retry(status: int) -> None:
    service = GarminStrategy().data_247
    with (
        patch(f"{_MODULE}.download_binary_content", side_effect=ProviderError("garmin", upstream_status=status)),
        patch(f"{_MODULE}.schedule_fit_retry") as schedule,
    ):
        assert service.process_items_batch(MagicMock(), uuid4(), "activityFiles", [_ITEM]) == 0
    schedule.assert_not_called()
