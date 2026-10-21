"""Fictional WHOOP HTTP -> import -> PostgreSQL -> authenticated OW API.

Failure cases: open/pending/null vs zero, corrections and stale delivery, midnight
intervals, duplicate imports, completed totals, paging, ownership and invalid auth.
The exported envelopes are consumed by Rumi's wearable E2E runner.
"""

import json
import os
import subprocess
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Barrier, Thread
from typing import Any
from urllib.parse import parse_qs, urlparse
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from psycopg.errors import NumericValueOutOfRange
from sqlalchemy import Engine, delete, select
from sqlalchemy.orm import Session

from app.models import DataPointSeries, DataSource, DataTypeCoverage, HealthScore, ProviderCycle, ProviderPriority, User
from app.schemas.enums import HealthScoreCategory, ProviderName, SeriesType, get_series_type_id
from app.services.providers.whoop.data_247 import Whoop247Data
from app.services.providers.whoop.strategy import WhoopStrategy
from tests.factories import ApiKeyFactory, DataSourceFactory, EventRecordFactory, UserConnectionFactory, UserFactory
from tests.utils import api_key_headers


def test_whoop_cycle_http_journey(client: TestClient, db: Session, tmp_path: Path, engine: Engine) -> None:
    date = (datetime.now(timezone(timedelta(hours=3))) - timedelta(days=1)).date().isoformat()
    previous = (datetime.fromisoformat(date) - timedelta(days=1)).date().isoformat()
    raw: dict[str, Any] = {
        "id": 9001,
        "start": f"{previous}T23:30:00+03:00",
        "end": None,
        "updated_at": f"{date}T09:00:00+03:00",
        "timezone_offset": "+03:00",
        "score_state": "SCORED",
        "step_count": 1000,
        "score": {"strain": 6.5, "kilojoule": 4184, "average_heart_rate": 70, "max_heart_rate": 140},
    }
    state = {"records": [raw]}
    requests: list[str] = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            requests.append(self.path)
            assert self.headers.get("Authorization") == "Bearer fictional-whoop-token"
            page = int(parse_qs(urlparse(self.path).query).get("nextToken", ["0"])[0])
            body = json.dumps(
                {
                    "records": state["records"][page : page + 1],
                    "next_token": str(page + 1) if page + 1 < len(state["records"]) else None,
                }
            ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: Any) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    user, other = UserFactory(), UserFactory()
    UserConnectionFactory(user=user, provider="whoop", access_token="fictional-whoop-token")
    headers = api_key_headers(ApiKeyFactory())
    strategy = WhoopStrategy()
    data = Whoop247Data("whoop", f"http://127.0.0.1:{server.server_port}", strategy.oauth)
    start = datetime.fromisoformat(f"{previous}T00:00:00+03:00")
    end = datetime.fromisoformat(f"{date}T23:59:59+03:00")
    params = {"start_date": start.isoformat(), "end_date": end.isoformat(), "limit": 1}
    path = f"/api/v1/users/{user.id}/summaries/cycles"
    envelopes: dict[str, Any] = {}

    def pull() -> dict[str, Any]:
        result = data.refresh_cycles(db, user.id, start, end)
        assert not result.outcomes["cycles"].truncated
        response = client.get(path, params=params, headers=headers)
        assert response.status_code == 200, response.text
        return response.json()

    try:
        # Separate real connections race the same user's snapshot/projection writes.
        # This user is committed outside the ordinary rollback fixture so both see it.
        concurrent_user_id = uuid4()
        with Session(engine) as concurrent_db:
            existing_priority_ids = set(concurrent_db.scalars(select(ProviderPriority.id)))
            concurrent_db.add(User(id=concurrent_user_id, first_name="Concurrent fixture"))
            concurrent_db.commit()
        try:
            barrier = Barrier(2)

            def concurrent_write(hour: int, steps: int) -> None:
                revision = deepcopy(raw)
                revision.update(
                    id=9010,
                    updated_at=f"{date}T{hour:02d}:00:00+03:00",
                    step_count=steps,
                    end=f"{date}T14:00:00+03:00",
                    score={"strain": steps / 100, "kilojoule": 4184},
                )
                with Session(engine) as concurrent_db:
                    barrier.wait(timeout=10)
                    data._save_cycles(concurrent_db, concurrent_user_id, [revision])
                    concurrent_db.commit()

            with ThreadPoolExecutor(max_workers=2) as pool:
                futures = [pool.submit(concurrent_write, 20, 100), pool.submit(concurrent_write, 21, 200)]
                for future in futures:
                    future.result(timeout=20)
            concurrent_response = client.get(
                f"/api/v1/users/{concurrent_user_id}/summaries/cycles", params=params, headers=headers
            ).json()
            assert concurrent_response["data"][0]["steps"] == 200
            with Session(engine) as concurrent_db:
                actual = concurrent_db.scalars(
                    select(DataPointSeries.value)
                    .join(DataSource)
                    .where(
                        DataSource.user_id == concurrent_user_id,
                        DataPointSeries.series_type_definition_id == get_series_type_id(SeriesType.steps),
                    )
                ).all()
                assert [float(value) for value in actual] == [200.0]
        finally:
            with Session(engine) as concurrent_db:
                concurrent_db.execute(delete(User).where(User.id == concurrent_user_id))
                # Source creation may seed a global provider priority outside the
                # rollback fixture; remove only rows this concurrent phase created.
                concurrent_db.execute(
                    delete(ProviderPriority).where(
                        ProviderPriority.provider == ProviderName.WHOOP,
                        ProviderPriority.id.not_in(existing_priority_ids),
                    )
                )
                concurrent_db.commit()

        opened = pull()
        cycle = opened["data"][0]
        assert cycle["provisional"] is True
        assert cycle["end_time"] is None
        assert cycle["total_calories_kcal"] == 1000
        assert cycle["avg_heart_rate_bpm"] == 70
        assert db.scalars(select(DataPointSeries).join(DataSource).where(DataSource.user_id == user.id)).all() == []
        envelopes["open"] = opened
        raw.update(updated_at=f"{date}T12:00:00+03:00", step_count=1800)
        raw["score"]["strain"] = 8
        envelopes["growing"] = pull()
        assert envelopes["growing"]["data"][0]["steps"] == 1800
        raw.update(updated_at=f"{date}T13:00:00+03:00", step_count=1500)
        envelopes["corrected"] = pull()
        assert envelopes["corrected"]["data"][0]["steps"] == 1500
        raw.update(updated_at=f"{date}T11:00:00+03:00", step_count=9999)
        assert pull()["data"][0]["steps"] == 1500
        raw.update(updated_at=f"{date}T14:00:00+03:00", end=f"{date}T14:00:00+03:00", step_count=2000)
        envelopes["completed"] = pull()
        assert envelopes["completed"]["data"][0]["provisional"] is False
        pull()
        assert len(db.scalars(select(ProviderCycle).where(ProviderCycle.user_id == user.id)).all()) == 1
        pending = deepcopy(raw)
        pending.update(
            id=9002,
            start=f"{date}T14:00:00+03:00",
            end=None,
            updated_at=f"{date}T15:00:00+03:00",
            score_state="PENDING_SCORE",
            step_count=None,
            score=None,
        )
        zero = deepcopy(pending)
        zero.update(id=9003, updated_at=f"{date}T16:00:00+03:00", score_state="UNSCORABLE", step_count=0)
        state["records"] = [raw, pending, zero]
        first = pull()
        second = client.get(
            path, params={**params, "cursor": first["pagination"]["next_cursor"]}, headers=headers
        ).json()
        third = client.get(
            path, params={**params, "cursor": second["pagination"]["next_cursor"]}, headers=headers
        ).json()
        assert second["data"][0]["steps"] is None
        assert second["data"][0]["strain"] is None
        assert third["data"][0]["steps"] == 0
        assert third["data"][0]["strain"] is None
        assert third["pagination"]["has_more"] is False
        assert client.get(path, params=params).status_code == 401
        assert client.get(path, params={**params, "cursor": "invalid"}, headers=headers).status_code == 400
        assert (
            client.get(f"/api/v1/users/{other.id}/summaries/cycles", params=params, headers=headers).json()["data"]
            == []
        )
        # Closed projections must obey the same revision order as canonical cycles.
        day = data.normalize_cycle(raw, user.id)[0][0].recorded_at
        step_type = get_series_type_id(SeriesType.steps)
        energy_type = get_series_type_id(SeriesType.total_energy)

        def projection() -> tuple[dict[int, float], list[float]]:
            db.expire_all()
            samples = db.execute(
                select(DataPointSeries.series_type_definition_id, DataPointSeries.value)
                .join(DataSource)
                .where(
                    DataSource.user_id == user.id,
                    DataSource.provider == ProviderName.WHOOP,
                    DataSource.source == "whoop",
                    DataSource.device_model.is_(None),
                    DataPointSeries.recorded_at == day,
                )
            ).all()
            scores = db.scalars(
                select(HealthScore.value).where(
                    HealthScore.user_id == user.id,
                    HealthScore.provider == ProviderName.WHOOP,
                    HealthScore.category == HealthScoreCategory.STRAIN,
                    HealthScore.event_record_id.is_(None),
                    HealthScore.recorded_at == day,
                )
            ).all()
            return {kind: round(float(value), 2) for kind, value in samples}, [
                float(value) for value in scores if value is not None
            ]

        protected = []
        for owner, provider, device in (
            (user, "whoop", "another-device"),
            (user, "apple", None),
            (other, "whoop", None),
        ):
            source = DataSourceFactory(user=owner, provider=provider, source=provider, device_model=device)
            sample = DataPointSeries(
                id=uuid4(),
                data_source_id=source.id,
                series_type_definition_id=step_type,
                recorded_at=day,
                value=777,
                is_daily_total=True,
            )
            db.add(sample)
            protected.append(sample.id)
        workout = EventRecordFactory(category="workout")
        workout_strain = HealthScore(
            id=uuid4(),
            user_id=user.id,
            provider=ProviderName.WHOOP,
            category=HealthScoreCategory.STRAIN,
            value=5,
            recorded_at=day + timedelta(hours=1),
            event_record_id=workout.id,
        )
        db.add(workout_strain)
        db.commit()

        state["records"] = [raw]
        raw.update(updated_at=f"{date}T17:00:00+03:00", step_count=1700)
        raw["score"].update(strain=7, kilojoule=2092)
        pull()
        assert projection() == ({step_type: 1700.0, energy_type: 500.0}, [7.0])
        raw.update(updated_at=f"{date}T16:00:00+03:00", step_count=9999)
        raw["score"].update(strain=19, kilojoule=41840)
        pull()
        assert projection() == ({step_type: 1700.0, energy_type: 500.0}, [7.0])
        assert data.load_closed_cycle(db, user.id, datetime.fromisoformat(raw["end"])) == 1
        assert projection() == ({step_type: 1700.0, energy_type: 500.0}, [7.0])

        # A failed daily write after deleting the owned projection rolls the
        # canonical revision and the old daily readings back together.
        raw.update(updated_at=f"{date}T17:30:00+03:00", step_count=10**15)
        with pytest.raises(NumericValueOutOfRange):
            data.refresh_cycles(db, user.id, start, end)
        assert projection() == ({step_type: 1700.0, energy_type: 500.0}, [7.0])
        assert db.scalars(select(ProviderCycle).where(ProviderCycle.id == "9001")).one().payload["steps"] == 1700

        raw.update(updated_at=f"{date}T18:00:00+03:00", step_count=None)
        raw["score"] = None
        assert data.load_closed_cycle(db, user.id, datetime.fromisoformat(raw["end"])) == 1
        assert projection() == ({}, [])
        assert db.get(DataTypeCoverage, (user.id, "whoop", "total_energy", "series")) is None
        # Replaying a withdrawal cannot revive old values.
        pull()
        assert projection() == ({}, [])

        raw.update(updated_at=f"{date}T19:00:00+03:00", step_count=0)
        raw["score"] = {"strain": 0, "kilojoule": 4184}
        pull()
        assert projection() == ({step_type: 0.0, energy_type: 1000.0}, [0.0])
        # An overlapping short cycle is retained canonically, but never adds to or
        # takes ownership from the cycle covering most of this legacy day.
        overlapping = deepcopy(raw)
        overlapping.update(
            id=9004,
            start=f"{date}T08:00:00+03:00",
            end=f"{date}T10:00:00+03:00",
            updated_at=f"{date}T20:00:00+03:00",
            step_count=400,
        )
        overlapping["score"]["strain"] = 4
        state["records"] = [overlapping, raw]
        pull()
        assert projection() == ({step_type: 0.0, energy_type: 1000.0}, [0.0])
        overlapping.update(updated_at=f"{date}T21:00:00+03:00", step_count=None, score=None)
        state["records"] = [raw, overlapping]
        pull()
        assert projection() == ({step_type: 0.0, energy_type: 1000.0}, [0.0])
        assert db.scalars(select(ProviderCycle).where(ProviderCycle.id == "9004")).one().payload["steps"] is None
        assert all(float(db.get(DataPointSeries, key).value) == 777 for key in protected)
        assert float(db.get(HealthScore, workout_strain.id).value) == 5

        moved = deepcopy(raw)
        moved.update(
            id=9005,
            start=f"{previous}T00:00:00+03:00",
            end=f"{previous}T12:00:00+03:00",
            updated_at=f"{date}T21:15:00+03:00",
            step_count=100,
        )
        state["records"] = [moved]
        pull()
        owned_step_id = data._projection_id(user.id, "9005", "steps")
        assert db.get(DataPointSeries, owned_step_id) is not None
        moved.update(start=f"{date}T00:00:00+03:00", end=f"{date}T12:00:00+03:00", updated_at=f"{date}T21:30:00+03:00")
        pull()
        # Its old-day row disappears; the longer competing cycle still owns today.
        db.expire_all()
        assert db.get(DataPointSeries, owned_step_id) is None
        assert projection() == ({step_type: 0.0, energy_type: 1000.0}, [0.0])

        # Unknown pre-upgrade daily ownership must remain intact. This is a deliberate
        # compatibility boundary, not evidence that historical daily rows are repaired.
        legacy_user = UserFactory()
        UserConnectionFactory(user=legacy_user, provider="whoop", access_token="fictional-whoop-token")
        legacy_source = DataSourceFactory(user=legacy_user, provider="whoop", source="whoop", device_model=None)
        legacy_sample = DataPointSeries(
            id=uuid4(),
            data_source_id=legacy_source.id,
            series_type_definition_id=step_type,
            recorded_at=day,
            value=321,
            is_daily_total=True,
        )
        legacy_score = HealthScore(
            id=uuid4(),
            user_id=legacy_user.id,
            provider=ProviderName.WHOOP,
            category=HealthScoreCategory.STRAIN,
            value=3,
            recorded_at=day,
        )
        db.add_all([legacy_sample, legacy_score])
        db.commit()
        raw.update(updated_at=f"{date}T22:00:00+03:00", step_count=50)
        raw["score"]["strain"] = 1
        state["records"] = [raw]
        data.refresh_cycles(db, legacy_user.id, start, end)
        raw.update(updated_at=f"{date}T23:00:00+03:00", step_count=None, score=None)
        data.refresh_cycles(db, legacy_user.id, start, end)
        legacy_response = client.get(
            f"/api/v1/users/{legacy_user.id}/summaries/cycles", params=params, headers=headers
        ).json()
        assert legacy_response["data"][0]["steps"] is None
        assert legacy_response["data"][0]["strain"] is None
        db.expire_all()
        assert float(db.get(DataPointSeries, legacy_sample.id).value) == 321
        assert float(db.get(HealthScore, legacy_score.id).value) == 3
        projection_checks = {
            "closed_downward_correction": True,
            "concurrent_same_user_revisions": True,
            "failed_projection_atomic_rollback": True,
            "corrected_day_removes_old_projection": True,
            "closed_stale_poll_and_webhook": True,
            "null_withdrawal_and_replay": True,
            "zero_preserved": True,
            "coverage_narrowed": True,
            "same_day_competing_cycles": True,
            "other_sources_users_workouts_preserved": True,
            "unknown_legacy_rows_preserved": True,
            "legacy_conflict_canonical_null": True,
        }
        output = Path(os.environ.get("OW_CYCLE_E2E_OUTPUT", str(tmp_path / "cycle-journey.json")))
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(
            json.dumps(
                {
                    "revision": subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip(),
                    "passed": True,
                    "fictional_data": True,
                    "date": date,
                    "path": "HTTP WHOOP fixture -> provider import -> PostgreSQL -> authenticated OW API",
                    "requests": len(requests),
                    "envelopes": envelopes,
                    "projection_checks": projection_checks,
                },
                indent=2,
            )
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
