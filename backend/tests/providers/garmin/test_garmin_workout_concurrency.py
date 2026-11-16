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
from app.services.providers.garmin.webhook_handler import GarminWebhookHandler
from app.services.providers.garmin.workouts import GarminWorkouts
from tests.factories import UserConnectionFactory, UserFactory
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
def committed_users(engine: Engine) -> Generator[Callable[[str | None], UUID], None, None]:
    """Commits a user, connected to Garmin under the given Garmin user id when one is given. Each is deleted
    afterwards with the provider priorities their first data sources seed."""
    with Session(bind=engine) as session:
        priorities = list(session.execute(text("SELECT id FROM provider_priority")).scalars())
    user_ids: list[UUID] = []

    def create(garmin_user_id: str | None = None) -> UUID:
        with Session(bind=engine) as session:
            user = UserFactory.build()
            session.add(user)
            if garmin_user_id:
                session.add(UserConnectionFactory.build(user_id=user.id, provider_user_id=garmin_user_id))
            session.commit()
            user_ids.append(user.id)
            return user.id

    try:
        yield create
    finally:
        with Session(bind=engine) as session:
            session.execute(text('DELETE FROM "user" WHERE id = ANY(:ids)'), {"ids": user_ids})
            session.execute(text("DELETE FROM provider_priority WHERE NOT (id = ANY(:kept))"), {"kept": priorities})
            session.commit()


@pytest.fixture
def committed_user(committed_users: Callable[[str | None], UUID]) -> UUID:
    return committed_users(None)


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


def test_two_pushes_each_carrying_one_users_details_and_the_others_fit_file_store_both(
    engine: Engine, committed_users: Callable[[str | None], UUID], garmin: tuple[Garmin247Data, GarminWorkouts]
) -> None:
    """Garmin can deliver one user's activityDetails with another user's FIT file, and a second push the other
    way round. Each push locks its details' workout, then waits on the other push's lock for its FIT file."""
    data_247, workouts = garmin
    users = {"garmin_user_a": committed_users("garmin_user_a"), "garmin_user_b": committed_users("garmin_user_b")}

    def details(garmin_user_id: str, activity_id: int, start: int = _RUN["startTimeInSeconds"]) -> dict[str, Any]:
        summary = {**_RUN, "activityId": activity_id, "startTimeInSeconds": start, "steps": 8240}
        return {"userId": garmin_user_id, "activityId": activity_id, "summary": summary}

    def fit_file(garmin_user_id: str, activity_id: int) -> dict[str, Any]:
        return {**_PUSHES["activityFiles"], "userId": garmin_user_id, "activityId": activity_id}

    payloads = [
        {"activityDetails": [details("garmin_user_a", 321)], "activityFiles": [fit_file("garmin_user_b", 654)]},
        {"activityDetails": [details("garmin_user_b", 654)], "activityFiles": [fit_file("garmin_user_a", 321)]},
    ]
    both_downloading = threading.Barrier(2, timeout=_WAIT_SECONDS)

    def download(**_kwargs: Any) -> bytes:
        both_downloading.wait()
        return make_running_fit()

    handler = GarminWebhookHandler(workouts, data_247, data_247.oauth)
    factory = sessionmaker(bind=engine, autoflush=False)
    # An earlier workout of each user, so the race does not first create their device and its provider priority
    for garmin_user_id in users:
        with factory() as session:
            day_before = _RUN["startTimeInSeconds"] - 86400
            handler.process_payload(session, {"activityDetails": [details(garmin_user_id, 1, day_before)]}, "trace")
    results: list[dict[str, Any]] = []
    errors: list[BaseException] = []

    def deliver(payload: dict[str, Any]) -> None:
        with factory() as session:
            try:
                results.append(handler.process_payload(session, payload, "trace"))
            except BaseException as e:
                errors.append(e)

    with (
        patch("app.services.providers.garmin.data_247.download_binary_content", side_effect=download),
        patch("app.services.providers.garmin.data_247.store_fit_file"),
    ):
        threads = [threading.Thread(target=deliver, args=(payload,)) for payload in payloads]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(_WAIT_SECONDS * 2)

    assert not any(thread.is_alive() for thread in threads)
    assert not errors, errors
    assert [result["errors"] for result in results] == [[], []]
    for garmin_user_id, activity_id in (("garmin_user_a", "321"), ("garmin_user_b", "654")):
        [(record, detail, source)] = [w for w in _workouts(engine, users[garmin_user_id]) if w[0].external_id != "1"]
        assert (record.external_id, source.device_model) == (activity_id, "forerunner965")
        assert detail is not None
        assert (detail.steps_count, detail.aerobic_training_effect) == (8240, Decimal("3.4"))
        assert detail.segments
