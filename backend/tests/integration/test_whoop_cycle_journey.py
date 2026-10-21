"""Fictional WHOOP HTTP -> import -> PostgreSQL -> authenticated OW API.

Failure cases: open/pending/null vs zero, corrections and stale delivery, midnight
intervals, duplicate imports, completed totals, paging, ownership and invalid auth.
The exported envelopes are consumed by Rumi's wearable E2E runner.
"""

import json
import os
import subprocess
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread
from typing import Any
from urllib.parse import parse_qs, urlparse

from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import DataPointSeries, DataSource, ProviderCycle
from app.services.providers.whoop.data_247 import Whoop247Data
from app.services.providers.whoop.strategy import WhoopStrategy
from tests.factories import ApiKeyFactory, UserConnectionFactory, UserFactory
from tests.utils import api_key_headers


def test_whoop_cycle_http_journey(client: TestClient, db: Session, tmp_path: Path) -> None:
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
                },
                indent=2,
            )
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
