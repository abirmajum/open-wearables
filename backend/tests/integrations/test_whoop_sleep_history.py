"""WHOOP fixture -> real PostgreSQL persistence -> public API evidence.

Failure cases: one provider session connected to two OW users; retry and moved
session timestamps; pre-existing unlinked scores; failed persistence reported as
success; unscored values shown as zero; and detail lost at the response boundary.
The JSON report contains only synthetic fixtures. No provider credentials are used.
"""

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch
from uuid import UUID, uuid4

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models import EventRecord, HealthScore
from app.schemas.enums import ProviderName
from app.services.health_score_service import health_score_service
from app.services.providers.whoop.data_247 import Whoop247Data
from tests.factories import ApiKeyFactory, DataSourceFactory, EventRecordFactory, UserFactory, WorkoutDetailsFactory
from tests.utils import api_key_headers


def sleep_fixture() -> dict[str, Any]:
    return {
        "id": str(uuid4()),
        "cycle_id": 101,
        "start": "2026-09-19T22:00:00Z",
        "end": "2026-09-20T06:00:00Z",
        "timezone_offset": "+03:00",
        "nap": False,
        "score_state": "SCORED",
        "score": {
            "sleep_performance_percentage": 81,
            "sleep_consistency_percentage": 75,
            "sleep_efficiency_percentage": 93,
            "respiratory_rate": 14.5,
            "sleep_needed": {"baseline_milli": 28800000, "need_from_sleep_debt_milli": 3600000},
            "stage_summary": {
                "total_light_sleep_time_milli": 14400000,
                "total_deep_sleep_time_milli": 7200000,
                "total_rem_sleep_time_milli": 5400000,
                "total_awake_time_milli": 1800000,
                "sleep_cycle_count": 4,
                "disturbance_count": 3,
            },
        },
    }


def test_whoop_sleep_import_to_public_api(client: TestClient, db: Session, tmp_path: Path) -> None:
    provider = Whoop247Data("whoop", "https://fictional.example", MagicMock())
    users = [UserFactory(), UserFactory()]
    raw = sleep_fixture()
    start, end = datetime(2026, 9, 18, tzinfo=timezone.utc), datetime(2026, 9, 25, tzinfo=timezone.utc)
    headers = api_key_headers(ApiKeyFactory())
    # Simulate the old global UUID row and old unlinked score for the first user.
    normalized, score = provider.normalize_sleep(raw, users[0].id)
    normalized["id"] = UUID(raw["id"])
    legacy_id = provider.save_sleep_data(db, users[0].id, normalized)
    assert score is not None
    legacy_score = health_score_service.create(db, score)
    legacy_score_id = legacy_score.id
    evidence: dict[str, Any] = {"fictional_data": True, "accounts": [], "checks": []}
    for user in users:
        with patch.object(provider, "_fetch_paginated", return_value=([raw], False)):
            assert provider.load_and_save_sleep(db, user.id, start, end) == (1, False)
            assert provider.load_and_save_sleep(db, user.id, start, end) == (1, False)
        params = {"start_date": start.isoformat(), "end_date": end.isoformat()}
        response = client.get(f"/api/v1/users/{user.id}/events/sleep", params=params, headers=headers)
        assert response.status_code == 200, response.text
        sessions = response.json()["data"]
        assert len(sessions) == 1
        saved_id = UUID(sessions[0]["id"])
        summary = client.get(f"/api/v1/users/{user.id}/summaries/sleep", params=params, headers=headers)
        assert summary.status_code == 200, summary.text
        assert summary.json()["data"][0]["sessions"][0]["id"] == str(saved_id)
        saved_score = db.query(HealthScore).filter_by(user_id=user.id, provider=ProviderName.WHOOP).one()
        assert saved_score.event_record_id == saved_id
        assert saved_score.components["need_from_sleep_debt_milli"]["value"] == 3600000
        assert saved_score.components["score_state"]["qualifier"] == "SCORED"
        assert saved_score.zone_offset == "+03:00"
        evidence["accounts"].append({"session": sessions[0], "summary": summary.json()["data"][0]})
        if user == users[0]:
            assert saved_id == legacy_id
            assert saved_score.id == legacy_score_id
        else:
            assert saved_id != legacy_id
    evidence["checks"].extend(
        [
            "two accounts preserve distinct sessions",
            "legacy IDs preserved",
            "retry idempotence",
            "score linked",
            "summary IDs",
        ]
    )

    # A correction outside the adjacency threshold updates the existing row.
    raw["start"], raw["end"] = "2026-09-21T22:00:00Z", "2026-09-22T06:00:00Z"
    raw["score"]["sleep_performance_percentage"] = 91
    with patch.object(provider, "_fetch_paginated", return_value=([raw], False)):
        assert provider.load_and_save_sleep(db, users[0].id, start, end) == (1, False)
    db.expire_all()
    corrected = db.query(HealthScore).filter_by(id=legacy_score_id).one()
    assert corrected.value == 91
    assert corrected.event_record_id == legacy_id
    assert corrected.recorded_at.date().isoformat() == "2026-09-21"
    assert db.query(EventRecord).filter_by(external_id=raw["id"]).count() == 2
    evidence["checks"].append("corrections update in place")

    # Pending scoring is explicit null, including after an earlier scored result.
    raw["score_state"] = "PENDING_SCORE"
    with patch.object(provider, "_fetch_paginated", return_value=([raw], False)):
        assert provider.load_and_save_sleep(db, users[0].id, start, end) == (1, False)
    db.expire_all()
    assert db.get(HealthScore, legacy_score_id).value is None
    evidence["checks"].append("pending score remains null")

    nap = {
        **raw,
        "id": str(uuid4()),
        "nap": True,
        "start": "2026-09-22T06:30:00Z",
        "end": "2026-09-22T07:00:00Z",
        "score": {},
    }
    with patch.object(provider, "_fetch_paginated", return_value=([raw, nap], False)):
        assert provider.load_and_save_sleep(db, users[0].id, start, end) == (2, False)
    response = client.get(
        f"/api/v1/users/{users[0].id}/events/sleep",
        params={"start_date": start.isoformat(), "end_date": end.isoformat()},
        headers=headers,
    )
    assert response.status_code == 200
    assert len(response.json()["data"]) == 2
    assert sum(row["is_nap"] for row in response.json()["data"]) == 1
    evidence["checks"].append("adjacent WHOOP naps remain separate sessions")

    # Stored workout zones survive the public response model.
    source = DataSourceFactory(user=users[0], provider=ProviderName.WHOOP, source="whoop")
    workout = EventRecordFactory(data_source=source, start_datetime=start, end_datetime=start.replace(hour=1))
    zones = {"zones": [{"zone": 2, "seconds": 900.0, "max_bpm": None}], "max_hr": None, "threshold_hr": None}
    WorkoutDetailsFactory(event_record=workout, hr_zones=zones)
    db.commit()
    response = client.get(
        f"/api/v1/users/{users[0].id}/events/workouts",
        params={"start_date": start.isoformat(), "end_date": end.isoformat()},
        headers=headers,
    )
    assert response.status_code == 200, response.text
    assert response.json()["data"][0]["hr_zones"] == zones
    evidence["checks"].append("workout zones exposed")
    (tmp_path / "whoop-history-evidence.json").write_text(json.dumps(evidence, indent=2), encoding="utf-8")


def test_failed_sleep_persistence_reports_partial(db: Session, tmp_path: Path) -> None:
    provider = Whoop247Data("whoop", "https://fictional.example", MagicMock())
    user = UserFactory()
    raw = sleep_fixture()
    with (
        patch.object(provider, "_fetch_paginated", return_value=([raw], False)),
        patch(
            "app.services.providers.whoop.data_247.event_record_service.create_or_merge_sleep",
            side_effect=RuntimeError("fictional persistence failure"),
        ),
    ):
        result = provider.load_and_save_sleep(db, user.id, datetime.now(timezone.utc), datetime.now(timezone.utc))
    assert result == (0, True)
    assert db.query(EventRecord).filter_by(external_id=raw["id"]).count() == 0
    (tmp_path / "whoop-failure-evidence.json").write_text(
        json.dumps({"fictional_data": True, "saved": 0, "partial": True}), encoding="utf-8"
    )
