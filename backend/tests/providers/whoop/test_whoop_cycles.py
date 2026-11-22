"""Canonical WHOOP revisions retain open cycles without projecting provisional daily totals."""

from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

from sqlalchemy.orm import Session

from app.models import HealthScore, SeriesSample
from app.services.provider_cycle_service import provider_cycle_service
from app.services.providers.whoop.data_247 import Whoop247Data
from tests.factories import UserFactory


def _cycle(strain: float, revision: int, *, closed: bool = False) -> dict:
    return {
        "id": 93845,
        "start": "2026-10-12T21:30:00Z",
        "end": "2026-10-13T22:05:00Z" if closed else None,
        "updated_at": f"2026-10-14T00:00:0{revision}Z",
        "timezone_offset": "+03:00",
        "score_state": "SCORED",
        "score": {"strain": strain, "kilojoule": 5200.0},
    }


def test_open_cycle_updates_canonical_snapshot_but_not_daily_series(db: Session) -> None:
    user = UserFactory()
    provider = Whoop247Data("whoop", "https://fictional.example", MagicMock())
    start = datetime(2026, 10, 12, tzinfo=timezone.utc)
    end = datetime(2026, 10, 15, tzinfo=timezone.utc)
    for revision, strain in [(1, 3.1), (2, 8.7)]:
        with patch.object(provider, "_fetch_paginated", return_value=([_cycle(strain, revision)], False)):
            count, partial = provider.load_and_save_cycles(db, user.id, start, end)
        assert count == 1
        assert not partial
        [snapshot] = provider_cycle_service.list_whoop(db, user.id)
        assert snapshot.id == "93845"
        assert snapshot.strain == strain
        assert snapshot.end_time is None
        assert snapshot.provisional
        assert db.query(HealthScore).filter_by(user_id=user.id).count() == 0
        assert db.query(SeriesSample).count() == 0


def test_stale_open_snapshot_cannot_undo_closed_revision(db: Session) -> None:
    user = UserFactory()
    provider = Whoop247Data("whoop", "https://fictional.example", MagicMock())
    provider._save_cycles(db, user.id, [_cycle(14.2, 2, closed=True)])
    db.commit()
    provider._save_cycles(db, user.id, [_cycle(8.7, 1)])
    db.commit()
    [snapshot] = provider_cycle_service.list_whoop(db, user.id)
    assert snapshot.strain == 14.2
    assert not snapshot.provisional
    [strain] = db.query(HealthScore).filter_by(user_id=user.id).all()
    assert float(strain.value) == 14.2


def test_empty_cycles_do_not_write(db: Session) -> None:
    user = UserFactory()
    provider = Whoop247Data("whoop", "https://fictional.example", MagicMock())
    assert provider._save_cycles(db, user.id, []) == 0
    assert provider_cycle_service.list_whoop(db, user.id) == []
