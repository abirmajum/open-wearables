"""WHOOP cycle strain: the open cycle is stored and refreshed in place until it closes.

Regression: normalize_cycle dropped any cycle without an end because cycle strain
was written with ON CONFLICT DO NOTHING, so today's day strain never reached
consumers. Strain now upserts on the cycle start, so the partial value climbs and
the closed value replaces it. All payloads are synthetic.
"""

from datetime import datetime, timedelta, timezone
from typing import Any
from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest
from sqlalchemy.orm import Session

from app.models import DataPointSeries, DataSource, HealthScore
from app.repositories.health_score_repository import HealthScoreRepository
from app.schemas.enums import HealthScoreCategory, ProviderName, SeriesType, get_series_type_id
from app.schemas.model_crud.activities import HealthScoreCreate, ScoreComponent
from app.services.providers.whoop.data_247 import Whoop247Data
from tests.factories import DataSourceFactory, EventRecordFactory, UserFactory

CYCLE_START = "2026-10-12T21:30:00Z"
WINDOW = (datetime(2026, 10, 12, tzinfo=timezone.utc), datetime(2026, 10, 14, tzinfo=timezone.utc))


def cycle_fixture(strain: float, kilojoule: float, end: str | None = None, score_state: str = "SCORED") -> dict:
    return {
        "id": 93845,
        "user_id": 15469532,
        "start": CYCLE_START,
        "end": end,
        "timezone_offset": "+03:00",
        "score_state": score_state,
        "score": {"strain": strain, "kilojoule": kilojoule, "average_heart_rate": 70, "max_heart_rate": 150},
    }


@pytest.fixture
def provider() -> Whoop247Data:
    return Whoop247Data("whoop", "https://fictional.example", MagicMock())


def _strain_rows(db: Session, user_id: Any) -> list[HealthScore]:
    db.expire_all()
    return (
        db.query(HealthScore)
        .filter_by(user_id=user_id, provider=ProviderName.WHOOP, category=HealthScoreCategory.STRAIN)
        .all()
    )


def _energy_values(db: Session, user_id: Any) -> list[float]:
    rows = (
        db.query(DataPointSeries)
        .join(DataSource, DataPointSeries.data_source_id == DataSource.id)
        .filter(
            DataSource.user_id == user_id,
            DataPointSeries.series_type_definition_id == get_series_type_id(SeriesType.energy),
        )
        .all()
    )
    return [float(r.value) for r in rows]


def _pull(provider: Whoop247Data, db: Session, user_id: Any, raw: dict) -> tuple[int, bool]:
    with patch.object(provider, "_fetch_paginated", return_value=([raw], False)):
        return provider.load_and_save_cycles(db, user_id, *WINDOW)


def test_open_cycle_strain_stored_then_updated_in_place(db: Session, provider: Whoop247Data) -> None:
    user = UserFactory()

    _pull(provider, db, user.id, cycle_fixture(strain=3.1, kilojoule=2000.0))
    [first] = _strain_rows(db, user.id)
    first_id = first.id
    assert float(first.value) == 3.1
    assert first.components["cycle_state"]["qualifier"] == "OPEN"
    assert first.recorded_at == datetime(2026, 10, 12, 21, 30, tzinfo=timezone.utc)
    assert first.zone_offset == "+03:00"
    assert len(_energy_values(db, user.id)) == 1

    # Later pull: the cycle is still open and strain has climbed.
    _pull(provider, db, user.id, cycle_fixture(strain=8.7, kilojoule=5200.0))
    [updated] = _strain_rows(db, user.id)
    assert updated.id == first_id
    assert float(updated.value) == 8.7
    assert updated.components["kilojoule"]["value"] == 5200.0
    assert updated.components["cycle_state"]["qualifier"] == "OPEN"
    [energy] = _energy_values(db, user.id)
    assert energy == pytest.approx(5200.0 / 4.184, abs=0.01)


def test_closed_cycle_overwrites_partial_value(db: Session, provider: Whoop247Data) -> None:
    user = UserFactory()
    _pull(provider, db, user.id, cycle_fixture(strain=8.7, kilojoule=5200.0))
    [partial] = _strain_rows(db, user.id)

    # The cycle closes via the webhook path (sleep.updated -> load_single_cycle).
    closed = cycle_fixture(strain=14.2, kilojoule=9100.0, end="2026-10-13T22:05:00Z")
    with patch.object(provider, "get_cycle_record", return_value=closed):
        assert provider.load_single_cycle(db, user.id, "93845") == 1

    [final] = _strain_rows(db, user.id)
    assert final.id == partial.id
    assert float(final.value) == 14.2
    assert final.components["cycle_state"]["qualifier"] == "CLOSED"
    [energy] = _energy_values(db, user.id)
    assert energy == pytest.approx(9100.0 / 4.184, abs=0.01)

    # Replaying the closed cycle is idempotent and writes nothing.
    _pull(provider, db, user.id, closed)
    [replayed] = _strain_rows(db, user.id)
    assert replayed.id == partial.id
    assert float(replayed.value) == 14.2


def test_closed_cycle_replaces_partial_on_pull(db: Session, provider: Whoop247Data) -> None:
    user = UserFactory()
    _pull(provider, db, user.id, cycle_fixture(strain=2.0, kilojoule=1500.0))
    _pull(provider, db, user.id, cycle_fixture(strain=12.5, kilojoule=8000.0, end="2026-10-13T22:05:00Z"))
    [final] = _strain_rows(db, user.id)
    assert float(final.value) == 12.5
    assert final.components["cycle_state"]["qualifier"] == "CLOSED"


def test_unscored_cycle_is_skipped(db: Session, provider: Whoop247Data) -> None:
    user = UserFactory()
    _pull(provider, db, user.id, cycle_fixture(strain=1.0, kilojoule=900.0, score_state="PENDING_SCORE"))
    assert _strain_rows(db, user.id) == []
    assert _energy_values(db, user.id) == []


def test_pull_window_always_covers_recent_cycles(db: Session, provider: Whoop247Data) -> None:
    """An hourly pull's short cursor window still reaches the open and just-closed cycles."""
    user = UserFactory()
    end = datetime(2026, 10, 13, 12, 0, tzinfo=timezone.utc)
    with patch.object(provider, "_fetch_paginated", return_value=([], False)) as fetch:
        provider.load_and_save_cycles(db, user.id, end - timedelta(hours=1), end)
    assert fetch.call_args.args[3] == end - timedelta(days=2)

    # A wider (historical) window is left untouched.
    start = end - timedelta(days=30)
    with patch.object(provider, "_fetch_paginated", return_value=([], False)) as fetch:
        provider.load_and_save_cycles(db, user.id, start, end)
    assert fetch.call_args.args[3] == start


def test_period_upsert_never_overwrites_event_linked_score(db: Session) -> None:
    """A workout strain that shares the cycle's timestamp keeps its own value."""
    user = UserFactory()
    recorded_at = datetime(2026, 10, 12, 21, 30, tzinfo=timezone.utc)
    source = DataSourceFactory(user=user, provider=ProviderName.WHOOP, source="whoop")
    workout = EventRecordFactory(data_source=source, start_datetime=recorded_at)
    workout_score = HealthScore(
        id=uuid4(),
        user_id=user.id,
        provider=ProviderName.WHOOP,
        category=HealthScoreCategory.STRAIN,
        value=6.0,
        recorded_at=recorded_at,
        event_record_id=workout.id,
    )
    db.add(workout_score)
    db.flush()

    cycle_score = HealthScoreCreate(
        id=uuid4(),
        user_id=user.id,
        provider=ProviderName.WHOOP,
        category=HealthScoreCategory.STRAIN,
        value=13.0,
        recorded_at=recorded_at,
    )
    assert HealthScoreRepository(HealthScore).bulk_upsert_period_scores(db, [cycle_score]) == 0
    db.expire_all()
    assert float(db.get(HealthScore, workout_score.id).value) == 6.0


def test_period_upsert_rejects_event_linked_scores(db: Session) -> None:
    score = HealthScoreCreate(
        id=uuid4(),
        user_id=uuid4(),
        provider=ProviderName.WHOOP,
        category=HealthScoreCategory.STRAIN,
        value=1.0,
        recorded_at=datetime(2026, 10, 12, tzinfo=timezone.utc),
        event_record_id=uuid4(),
    )
    with pytest.raises(ValueError, match="period_score_must_not_link_event"):
        HealthScoreRepository(HealthScore).bulk_upsert_period_scores(db, [score])


def test_open_payload_never_replaces_closed_cycle(db: Session, provider: Whoop247Data) -> None:
    """A late or out-of-order fetch of the running cycle cannot undo the final strain or energy."""
    user = UserFactory()
    closed = cycle_fixture(strain=14.2, kilojoule=9100.0, end="2026-10-13T22:05:00Z")
    _pull(provider, db, user.id, closed)

    _pull(provider, db, user.id, cycle_fixture(strain=8.7, kilojoule=5200.0))

    [final] = _strain_rows(db, user.id)
    assert float(final.value) == 14.2
    assert final.components["cycle_state"]["qualifier"] == "CLOSED"
    [energy] = _energy_values(db, user.id)
    assert energy == pytest.approx(9100.0 / 4.184, abs=0.01)


def test_period_upsert_guard_keeps_closed_value(db: Session) -> None:
    """The repository guard holds even if a caller skips the closed-cycle check."""
    user = UserFactory()
    recorded_at = datetime(2026, 10, 12, 21, 30, tzinfo=timezone.utc)
    repo = HealthScoreRepository(HealthScore)

    def score(value: float, state: str) -> HealthScoreCreate:
        return HealthScoreCreate(
            id=uuid4(),
            user_id=user.id,
            provider=ProviderName.WHOOP,
            category=HealthScoreCategory.STRAIN,
            value=value,
            recorded_at=recorded_at,
            components={"cycle_state": ScoreComponent(qualifier=state)},
        )

    assert repo.bulk_upsert_period_scores(db, [score(14.2, "CLOSED")]) == 1
    assert repo.bulk_upsert_period_scores(db, [score(8.7, "OPEN")]) == 0
    assert repo.bulk_upsert_period_scores(db, [score(14.5, "CLOSED")]) == 1  # a closed revision still applies
    [row] = _strain_rows(db, user.id)
    assert float(row.value) == 14.5
