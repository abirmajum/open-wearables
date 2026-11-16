"""Two pushes of one Garmin workout processed at the same time leave one workout with every push's fields.

The ``db`` fixture runs a test inside one rolled-back transaction, where a second session could never see the first
one's rows nor wait on its locks. These tests open their own sessions on the test engine instead, commit for real,
and delete the user they created (its data sources, workouts and details cascade) at the end.
"""

import threading
import time
from collections.abc import Callable, Generator
from decimal import Decimal
from typing import Any
from unittest.mock import MagicMock, patch
from uuid import UUID

import pytest
from sqlalchemy import Engine, text
from sqlalchemy.orm import Session, sessionmaker

from app.models import DataSource, EventRecord, WorkoutDetails
from app.repositories.event_record_repository import EventRecordRepository
from app.repositories.user_connection_repository import UserConnectionRepository
from app.schemas.providers.garmin import ActivityJSON as GarminActivityJSON
from app.services.providers.garmin.data_247 import Garmin247Data
from app.services.providers.garmin.oauth import GarminOAuth
from app.services.providers.garmin.workouts import GarminWorkouts
from tests.factories import UserFactory
from tests.fixtures.fit_builder import make_running_fit

_RUN = {
    "activityId": 321,
    "activityType": "RUNNING",
    "startTimeInSeconds": 1748764800,
    "durationInSeconds": 20,
    "startTimeOffsetInSeconds": 7200,
    "deviceName": "forerunner965",
}
_PUSHES: dict[str, dict[str, Any]] = {
    "activities": {
        **_RUN,
        "userId": "garmin_user_123",
        "activityName": "Morning Run",
        "maxSpeedInMetersPerSecond": 4.1,
        "manual": False,
    },
    "activityDetails": {"activityId": 321, "summary": {**_RUN, "steps": 8240, "totalElevationGainInMeters": 84.0}},
    "activityFiles": {"activityId": 321, "fileType": "FIT", "callbackURL": "https://apis.garmin.com/file?id=321"},
}
_WAIT_SECONDS = 10.0


@pytest.fixture
def garmin() -> tuple[Garmin247Data, GarminWorkouts]:
    connection_repo = UserConnectionRepository()
    oauth = GarminOAuth(
        user_repo=MagicMock(),
        connection_repo=connection_repo,
        provider_name="garmin",
        api_base_url="https://apis.garmin.com",
    )
    data_247 = Garmin247Data(provider_name="garmin", api_base_url="https://apis.garmin.com", oauth=oauth)
    workouts = GarminWorkouts(
        workout_repo=EventRecordRepository(EventRecord),
        connection_repo=connection_repo,
        provider_name="garmin",
        api_base_url="https://apis.garmin.com",
        oauth=oauth,
    )
    return data_247, workouts


@pytest.fixture
def committed_user(engine: Engine) -> Generator[UUID, None, None]:
    """A committed user, deleted afterwards with the provider priority its first data source seeds."""
    with Session(bind=engine) as session:
        user = UserFactory.build()
        session.add(user)
        session.commit()
        user_id = user.id
        priorities = list(session.execute(text("SELECT id FROM provider_priority")).scalars())
    try:
        yield user_id
    finally:
        with Session(bind=engine) as session:
            session.execute(text('DELETE FROM "user" WHERE id = :id'), {"id": user_id})
            session.execute(text("DELETE FROM provider_priority WHERE NOT (id = ANY(:kept))"), {"kept": priorities})
            session.commit()


def _push(garmin: tuple[Garmin247Data, GarminWorkouts], summary_type: str, user_id: UUID) -> Callable[[Session], None]:
    """The push as its webhook processes it: activities commit themselves, the wellness types once processed."""
    data_247, workouts = garmin
    push = _PUSHES[summary_type]

    def run(session: Session) -> None:
        if summary_type == "activities":
            workouts.process_push_activities(session, [GarminActivityJSON(**push)], user_id)
        else:
            data_247.process_items_batch(session, user_id, summary_type, [push])
            session.commit()

    return run


def _waiting_locks(engine: Engine) -> int:
    with engine.connect() as connection:
        return connection.execute(text("SELECT count(*) FROM pg_locks WHERE NOT granted")).scalar_one()


def _race(engine: Engine, first: Callable[[Session], None], second: Callable[[Session], None]) -> None:
    """Run ``first`` until it commits, then ``second`` beside it, and let ``first`` commit once ``second`` has
    finished or waits on a lock."""
    factory = sessionmaker(bind=engine, autoflush=False)
    first_session, second_session = factory(), factory()
    at_commit, release = threading.Event(), threading.Event()
    commit = first_session.commit

    def held_commit() -> None:
        at_commit.set()
        assert release.wait(_WAIT_SECONDS), "first push was never let commit"
        commit()

    errors: list[BaseException] = []

    def guarded(work: Callable[[Session], None], session: Session) -> Callable[[], None]:
        def run() -> None:
            try:
                work(session)
            except BaseException as e:
                errors.append(e)
                session.rollback()
            finally:
                session.close()

        return run

    with (
        patch("app.services.providers.garmin.data_247.download_binary_content", return_value=make_running_fit()),
        patch("app.services.providers.garmin.data_247.store_fit_file"),
        patch.object(first_session, "commit", held_commit),
    ):
        first_thread = threading.Thread(target=guarded(first, first_session))
        second_thread = threading.Thread(target=guarded(second, second_session))
        first_thread.start()
        assert at_commit.wait(_WAIT_SECONDS), "first push never reached its commit"
        second_thread.start()
        deadline = time.monotonic() + _WAIT_SECONDS
        while second_thread.is_alive() and not _waiting_locks(engine) and time.monotonic() < deadline:
            time.sleep(0.02)
        release.set()
        first_thread.join(_WAIT_SECONDS)
        second_thread.join(_WAIT_SECONDS)

    assert not first_thread.is_alive()
    assert not second_thread.is_alive()
    assert not errors, errors


def _workouts(engine: Engine, user_id: UUID) -> list[tuple[EventRecord, WorkoutDetails | None, DataSource]]:
    with Session(bind=engine, expire_on_commit=False) as session:
        return (
            session.query(EventRecord, WorkoutDetails, DataSource)
            .join(DataSource, DataSource.id == EventRecord.data_source_id)
            .outerjoin(WorkoutDetails, WorkoutDetails.record_id == EventRecord.id)
            .filter(DataSource.user_id == user_id)
            .all()
        )


@pytest.mark.parametrize("order", [("activityFiles", "activities"), ("activities", "activityFiles")], ids="-".join)
def test_a_fit_file_and_its_activity_pushed_together_leave_one_workout(
    engine: Engine, committed_user: UUID, garmin: tuple[Garmin247Data, GarminWorkouts], order: tuple[str, str]
) -> None:
    _race(engine, _push(garmin, order[0], committed_user), _push(garmin, order[1], committed_user))

    [(record, detail, source)] = _workouts(engine, committed_user)
    assert (record.external_id, source.device_model) == ("321", "forerunner965")
    assert detail is not None
    assert (detail.label, detail.entry_source, detail.max_speed) == ("Morning Run", "automatic", Decimal("4.1"))
    assert (detail.training_load, detail.aerobic_training_effect) == (Decimal("142"), Decimal("3.4"))
    assert detail.segments


@pytest.mark.parametrize("order", [("activityDetails", "activities"), ("activities", "activityDetails")], ids="-".join)
def test_an_activity_and_its_details_pushed_together_leave_one_workout(
    engine: Engine, committed_user: UUID, garmin: tuple[Garmin247Data, GarminWorkouts], order: tuple[str, str]
) -> None:
    _race(engine, _push(garmin, order[0], committed_user), _push(garmin, order[1], committed_user))

    [(record, detail, source)] = _workouts(engine, committed_user)
    assert (record.external_id, source.device_model) == ("321", "forerunner965")
    assert detail is not None
    assert (detail.label, detail.entry_source, detail.max_speed) == ("Morning Run", "automatic", Decimal("4.1"))
    assert (detail.steps_count, detail.total_elevation_gain) == (8240, Decimal("84"))
