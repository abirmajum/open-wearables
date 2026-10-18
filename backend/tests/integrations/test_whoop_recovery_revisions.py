"""Synthetic WHOOP revisions through PostgreSQL and the APIs consumed by Rumi."""

from datetime import datetime, timezone
from typing import Any
from unittest.mock import MagicMock, patch
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models import DataPointSeries, DataSource, HealthScore
from app.schemas.enums import HealthScoreCategory, ProviderName, SeriesType, get_series_type_id
from app.services.health_score_service import health_score_service
from app.services.providers.whoop.data_247 import Whoop247Data
from tests.factories import ApiKeyFactory, UserFactory
from tests.utils import api_key_headers

START = datetime(2026, 10, 7, tzinfo=timezone.utc)
END = datetime(2026, 10, 9, tzinfo=timezone.utc)


def recovery_fixture(value: int = 50) -> dict[str, Any]:
    return {
        "cycle_id": 101,
        "sleep_id": str(uuid4()),
        "created_at": "2026-10-07T03:00:00Z",
        "updated_at": "2026-10-07T03:00:00Z",
        "score_state": "SCORED",
        "score": {
            "recovery_score": value,
            "resting_heart_rate": 60,
            "hrv_rmssd_milli": 45.5,
            "spo2_percentage": 97,
            "skin_temp_celsius": 33.2,
        },
    }


def ingest(provider: Whoop247Data, db: Session, user_id: UUID, raw: dict[str, Any], path: str) -> None:
    if path == "webhook":
        with patch.object(provider, "get_recovery_record", return_value=raw):
            provider.load_single_recovery(db, user_id, str(raw["cycle_id"]))
    else:
        with patch.object(provider, "_fetch_paginated", return_value=([raw], False)):
            _, partial = provider.load_and_save_recovery(db, user_id, START, END)
        assert partial is False


@pytest.mark.parametrize("path", ["webhook", "pull"])
def test_revisions_replace_legacy_score_in_public_apis(client: TestClient, db: Session, path: str) -> None:
    provider = Whoop247Data("whoop", "https://fictional.example", MagicMock())
    user, other_user = UserFactory(), UserFactory()
    headers = api_key_headers(ApiKeyFactory())
    raw = recovery_fixture()
    # Start with a row written by the old insert-only implementation.
    _, initial = provider.normalize_recovery(raw, user.id)
    assert initial is not None
    original_id = health_score_service.create(db, initial).id
    ingest(provider, db, other_user.id, raw, path)
    tomorrow = {**raw, "cycle_id": 102, "created_at": "2026-10-08T03:00:00Z"}
    ingest(provider, db, user.id, tomorrow, path)

    for revision, value in enumerate([70, 75, 40, 40], start=1):
        # A correction can lower the score; replaying the last result is harmless.
        # Even a next-day update must stay on the original recovery date.
        raw["updated_at"] = f"2026-10-08T0{revision}:00:00Z"
        raw["score"] = {"recovery_score": value, "resting_heart_rate": 55, "hrv_rmssd_milli": 66.5}
        ingest(provider, db, user.id, raw, path)
        db.expire_all()
        stored = db.get(HealthScore, original_id)
        assert stored is not None
        assert stored.value == value
        assert stored.recorded_at.isoformat() == "2026-10-07T03:00:00+00:00"
        assert "skin_temp_celsius" not in stored.components
        assert db.query(HealthScore).filter_by(user_id=user.id).count() == 2
        assert db.query(HealthScore).filter_by(user_id=other_user.id).one().value == 50
        tomorrow_time = datetime.fromisoformat(tomorrow["created_at"])
        assert db.query(HealthScore).filter_by(user_id=user.id, recorded_at=tomorrow_time).one().value == 50

        summaries = client.get(
            f"/api/v1/users/{user.id}/summaries/recovery",
            params={"start_date": START.isoformat(), "end_date": END.isoformat()},
            headers=headers,
        )
        assert summaries.status_code == 200, summaries.text
        today = next(row for row in summaries.json()["data"] if row["date"] == "2026-10-07")
        assert today["recovery_score"] == value
        assert today["resting_heart_rate_bpm"] == 55
        assert today["avg_hrv_rmssd_ms"] == 66.5
        assert today["avg_spo2_percent"] is None

        scores = client.get(f"/api/v1/users/{user.id}/health-scores", headers=headers)
        assert scores.status_code == 200, scores.text
        current = next(row for row in scores.json()["data"] if row["id"] == str(original_id))
        assert current["value"] == value
        assert current["components"]["hrv_rmssd_milli"]["value"] == 66.5
        sample = (
            db.query(DataPointSeries)
            .join(DataSource, DataPointSeries.data_source_id == DataSource.id)
            .filter(
                DataSource.user_id == user.id,
                DataPointSeries.series_type_definition_id
                == get_series_type_id(SeriesType.heart_rate_variability_rmssd),
                DataPointSeries.recorded_at == stored.recorded_at,
            )
            .one()
        )
        assert sample.value == 66.5


def test_pull_repairs_webhook_score_and_webhook_repairs_pull_score(db: Session) -> None:
    provider = Whoop247Data("whoop", "https://fictional.example", MagicMock())
    user = UserFactory()
    raw = recovery_fixture()
    for path, value in [("webhook", 50), ("pull", 70), ("webhook", 75)]:
        raw["score"]["recovery_score"] = value
        ingest(provider, db, user.id, raw, path)
        db.expire_all()
        assert db.query(HealthScore).filter_by(user_id=user.id).one().value == value


@pytest.mark.parametrize("path", ["webhook", "pull"])
def test_failed_score_write_can_be_retried(db: Session, path: str) -> None:
    provider = Whoop247Data("whoop", "https://fictional.example", MagicMock())
    user = UserFactory()
    raw = recovery_fixture()
    ingest(provider, db, user.id, raw, path)
    raw["score"]["recovery_score"] = 75
    with patch.object(health_score_service, "upsert_whoop_recovery", side_effect=RuntimeError("synthetic failure")):
        if path == "webhook":
            with pytest.raises(RuntimeError, match="synthetic failure"):
                ingest(provider, db, user.id, raw, path)
        else:
            with patch.object(provider, "_fetch_paginated", return_value=([raw], False)):
                assert provider.load_and_save_recovery(db, user.id, START, END) == (0, True)
    db.expire_all()
    assert db.query(HealthScore).filter_by(user_id=user.id).one().value == 50
    ingest(provider, db, user.id, raw, path)
    db.expire_all()
    assert db.query(HealthScore).filter_by(user_id=user.id).one().value == 75


@pytest.mark.parametrize("path", ["webhook", "pull"])
def test_unscored_recovery_does_not_replace_last_scored_value_with_zero(db: Session, path: str) -> None:
    provider = Whoop247Data("whoop", "https://fictional.example", MagicMock())
    user = UserFactory()
    raw = recovery_fixture(75)
    ingest(provider, db, user.id, raw, path)
    raw.update(score_state="PENDING_SCORE", score=None)
    ingest(provider, db, user.id, raw, path)
    db.expire_all()
    assert db.query(HealthScore).filter_by(user_id=user.id).one().value == 75


@pytest.mark.parametrize(
    ("field", "value"), [("provider", ProviderName.GARMIN), ("category", HealthScoreCategory.SLEEP)]
)
def test_recovery_upsert_rejects_other_score_types(db: Session, field: str, value: str) -> None:
    provider = Whoop247Data("whoop", "https://fictional.example", MagicMock())
    _, score = provider.normalize_recovery(recovery_fixture(), UserFactory().id)
    assert score is not None
    with pytest.raises(ValueError, match="whoop_recovery_required"):
        health_score_service.crud.upsert_whoop_recovery(db, score.model_copy(update={field: value}))
