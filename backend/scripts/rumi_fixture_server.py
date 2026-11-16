"""Serve Open Wearables' v1 API over synthetic Garmin and Ultrahuman data, for recording Rumi's coverage eval.

Starts a disposable Postgres and Redis (testcontainers), migrates the database like a deployment does, then runs
the seeds in ``scripts/rumi_fixture_seeds/`` through the real ingestion code:

- Garmin: every push is POSTed to the webhook route with the callback token; the Celery task that processes it
  runs inline. The FIT file an ``activityFiles`` push points at is served at the HTTP boundary.
- Ultrahuman: the ``sync_vendor_data`` task pulls every seeded day; the partner API is answered at the HTTP
  boundary from the seeds.

It then runs the post-processing production runs on a schedule (sleep and resilience scores, the day rollup),
prints one JSON line with the port, API key and users, and serves until killed. Nothing leaves this machine: any
other outbound HTTP request fails.

Usage (from ``backend/``, after ``uv sync --group dev``)::

    uv run python scripts/rumi_fixture_server.py --today 2026-11-15 [--port 8765] [--dump-to DIR] [--no-serve]
"""

import argparse
import json
import os
import random
import signal
import socket
import sys
import time as time_module
import uuid
from collections.abc import Callable, Generator
from contextlib import ExitStack, contextmanager, redirect_stdout
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any
from unittest.mock import patch

BACKEND_DIR = Path(__file__).resolve().parents[1]
SEEDS_DIR = BACKEND_DIR / "scripts" / "rumi_fixture_seeds"
DEFAULT_API_KEY = "sk-rumi-fixture-0000000000000000"
GARMIN_CLIENT_ID = "rumi-fixture-garmin-client"
# Seed epochs fall in this range; durations, offsets and ids are far smaller or far larger.
EPOCH_RANGE = range(1_500_000_000, 2_500_000_000)
DUMP_DAYS_BEFORE = 13


def _users() -> list[dict[str, Any]]:
    """The fixture users. Their ids are fixed so a recorded request path stays valid across runs."""
    return [
        {"label": "garmin", "id": "5eed0000-0000-4000-8000-000000000001", "garmin": "fixture-garmin-1"},
        {"label": "ultrahuman", "id": "5eed0000-0000-4000-8000-000000000002", "ultrahuman": True},
        {
            "label": "garmin_and_ultrahuman",
            "id": "5eed0000-0000-4000-8000-000000000003",
            "garmin": "fixture-garmin-3",
            "ultrahuman": True,
        },
        {
            "label": "ultrahuman_east",
            "id": "5eed0000-0000-4000-8000-000000000004",
            "ultrahuman": True,
            "ultrahuman_zone": "+04:00",
        },
    ]


def _ultrahuman_token(user: dict[str, Any]) -> str:
    """The user's Ultrahuman access token, which tells the offline API whose responses to answer with."""
    return f"rumi-fixture-ultrahuman-{user.get('ultrahuman_zone', '+00:00')}"


# ---------------------------------------------------------------------------
# Seeds
# ---------------------------------------------------------------------------


def _shift(value: Any, days: int) -> Any:
    """The seed with every epoch second and ISO date moved by ``days``; map keys stay as they are."""
    if isinstance(value, dict):
        return {k: _shift(v, days) for k, v in value.items()}
    if isinstance(value, list):
        return [_shift(v, days) for v in value]
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and value in EPOCH_RANGE:
        return value + days * 86400
    if isinstance(value, str) and len(value) == 10 and value[4] == "-" and value[7] == "-":
        try:
            return (date.fromisoformat(value) + timedelta(days=days)).isoformat()
        except ValueError:
            return value
    return value


def _load_seed(name: str, today: date) -> tuple[dict[str, Any], int]:
    seed = json.loads((SEEDS_DIR / f"{name}.json").read_text())
    days = (today - date.fromisoformat(seed["anchor_date"])).days
    return seed, days


def _garmin_pushes(today: date, garmin_user_id: str) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
    seed, days = _load_seed("garmin", today)
    raw = json.dumps(seed["pushes"]).replace(seed["user_id_placeholder"], garmin_user_id)
    return _shift(json.loads(raw), days), _shift(seed["fit_files"], days)


def _ultrahuman_responses(today: date) -> dict[str, dict[str, dict[str, Any]]]:
    """Each zone's responses by date; the UTC user's are the seed's ``responses``."""
    seed, days = _load_seed("ultrahuman", today)
    return {
        zone: {
            (date.fromisoformat(day) + timedelta(days=days)).isoformat(): _shift(response, days)
            for day, response in responses.items()
        }
        for zone, responses in {"+00:00": seed["responses"], **seed["responses_by_zone"]}.items()
    }


def _fit_file(spec: dict[str, Any]) -> bytes:
    """A FIT file with the activity's records and laps, built like the test fixtures build theirs."""
    sys.path.insert(0, str(BACKEND_DIR))
    from tests.fixtures.fit_builder import (
        _UINT8,
        _UINT16,
        _UINT32,
        _data_msg,
        _definition_msg,
        _Field,
        _fit_file,
        _fit_ts,
        _lap_messages,
        _session_message,
    )

    fields = [_Field(253, _UINT32, 4), _Field(3, _UINT8, 1), _Field(6, _UINT16, 2)]
    start = spec["start_time"]
    messages = _definition_msg(fields)
    for record in spec["records"]:
        ts = _fit_ts(datetime.fromtimestamp(start + record["offset"], tz=timezone.utc))
        messages += _data_msg(fields, [ts, record["heart_rate"], int(record["speed"] * 1000)])
    laps = []
    for lap in spec["laps"]:
        lap_start = _fit_ts(datetime.fromtimestamp(lap["start"], tz=timezone.utc))
        lap_end = _fit_ts(datetime.fromtimestamp(lap["end"], tz=timezone.utc))
        laps.append((lap_start, lap_end, lap_end - lap_start, lap["distance_meters"], lap["avg_hr"], lap["max_hr"]))
    messages += _lap_messages(laps)
    if session := spec.get("session"):
        end = _fit_ts(datetime.fromtimestamp(session["end"], tz=timezone.utc))
        messages += _session_message(
            end, session["aerobic_effect"], session["anaerobic_effect"], session["training_load"]
        )
    return _fit_file(messages)


# ---------------------------------------------------------------------------
# Environment
# ---------------------------------------------------------------------------


def _seeded_uuid4(seed: int) -> Callable[[], uuid.UUID]:
    rng = random.Random(seed)
    return lambda: uuid.UUID(int=rng.getrandbits(128), version=4)


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _configure_environment(db_url: Any, redis_host: str, redis_port: int, port: int) -> None:
    """Point the settings at the containers; must run before anything under ``app`` is imported."""
    os.environ.update(
        {
            "OW_IGNORE_ENV_FILE": "1",
            "ENVIRONMENT": "test",
            "SECRET_KEY": "rumi-fixture-secret-key-not-a-real-secret-0000",
            "MASTER_KEY": "cnVtaS1maXh0dXJlLW1hc3Rlci1rZXktbm90LXJlYWw=",
            "DB_HOST": db_url.host,
            "DB_PORT": str(db_url.port),
            "DB_NAME": db_url.database,
            "DB_USER": db_url.username,
            "DB_PASSWORD": db_url.password,
            "REDIS_HOST": redis_host,
            "REDIS_PORT": str(redis_port),
            "API_BASE_URL": f"http://127.0.0.1:{port}",
            "GARMIN_CLIENT_ID": GARMIN_CLIENT_ID,
            "GARMIN_CLIENT_SECRET": "rumi-fixture-garmin-secret",
            "ULTRAHUMAN_CLIENT_ID": "rumi-fixture-ultrahuman-client",
            "ULTRAHUMAN_CLIENT_SECRET": "rumi-fixture-ultrahuman-secret",
            "TELEMETRY_ENABLED": "false",
            "OTEL_ENABLED": "false",
            "LOG_LEVEL": "ERROR",
            # Deployed containers run on UTC; some ingestion code reads naive local time.
            "TZ": "UTC",
        }
    )
    time_module.tzset()
    os.chdir(BACKEND_DIR)
    sys.path.insert(0, str(BACKEND_DIR))


@contextmanager
def _containers() -> Generator[tuple[Any, str, int]]:
    from sqlalchemy.engine import make_url
    from testcontainers.postgres import PostgresContainer
    from testcontainers.redis import RedisContainer

    with (
        PostgresContainer(
            image="postgres:18", username="open-wearables", password="open-wearables", dbname="ow_rumi_fixture"
        ) as pg,
        RedisContainer(image="redis:7") as redis,
    ):
        yield (
            make_url(pg.get_connection_url()),
            redis.get_container_host_ip(),
            int(redis.get_exposed_port(6379)),
        )


def _offline_http(ultrahuman: dict[str, dict[str, dict[str, Any]]], fit_files: dict[str, bytes]) -> Any:
    """An httpx transport that answers the seeded provider requests and fails every other one; an Ultrahuman
    request gets the responses of its access token."""
    import httpx

    def handle(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url in fit_files:
            return httpx.Response(200, content=fit_files[url])
        if request.url.path.endswith("/user_data/metrics"):
            day = request.url.params.get("date", "")
            responses = ultrahuman[request.headers.get("Authorization", "").removeprefix("Bearer ")]
            return httpx.Response(200, json=responses.get(day, {"data": {"metric_data": []}, "status": 200}))
        raise httpx.ConnectError(f"rumi fixture server: no outbound requests ({request.method} {url})")

    return httpx.MockTransport(handle)


@contextmanager
def _ingestion_patches(transport: Any) -> Generator[list[str]]:
    """Run queued Celery work inline where production would, and keep HTTP on ``transport``."""
    import httpx
    from celery.app.base import Celery
    from celery.app.task import Task

    from app.constants.celery_tasks import CeleryTask
    from app.integrations.celery.tasks.webhook_push_task import process_webhook_push

    skipped: list[str] = []
    real_client = httpx.Client

    def send_task(_self: Any, name: str, args: Any = None, kwargs: Any = None, **_options: Any) -> None:
        if name == CeleryTask.PROCESS_WEBHOOK_PUSH:
            process_webhook_push(*(args or []), **(kwargs or {}))
        else:
            skipped.append(name)

    def apply_async(self: Any, args: Any = None, kwargs: Any = None, **_options: Any) -> None:
        skipped.append(self.name)

    with ExitStack() as stack:
        stack.enter_context(patch.object(Celery, "send_task", send_task))
        stack.enter_context(patch.object(Task, "apply_async", apply_async))
        stack.enter_context(
            patch(
                "app.services.providers.api_client.httpx.Client",
                lambda **kw: real_client(transport=transport, **{k: v for k, v in kw.items() if k != "http2"}),
            )
        )
        stack.enter_context(
            patch(
                "app.services.providers.api_client.httpx.get",
                lambda url, **kw: real_client(transport=transport).get(url, **kw),
            )
        )
        yield skipped


# ---------------------------------------------------------------------------
# Seeding
# ---------------------------------------------------------------------------


def _migrate_and_initialize() -> None:
    from alembic import command
    from alembic.config import Config

    from scripts.init.seed_archival_settings import seed_archival_settings
    from scripts.init.seed_series_types import seed_series_types
    from scripts.init_device_priorities import init_device_priorities
    from scripts.init_provider_priorities import init_provider_priorities
    from scripts.init_provider_settings import init_provider_settings

    command.upgrade(Config(str(BACKEND_DIR / "alembic.ini")), "head")
    for step in (
        init_provider_settings,
        init_device_priorities,
        init_provider_priorities,
        seed_series_types,
        seed_archival_settings,
    ):
        step()


def _create_users_and_key(api_key: str) -> None:
    from app.database import SessionLocal
    from app.repositories.user_connection_repository import UserConnectionRepository
    from app.schemas.model_crud.credentials import ApiKeyCreate
    from app.schemas.model_crud.user_management.user import UserCreateInternal
    from app.schemas.model_crud.user_management.user_connection import UserConnectionCreate
    from app.services.api_key_service import KEY_PREFIX_LENGTH, api_key_service
    from app.services.user_service import user_service
    from app.utils.security import hash_api_key

    connections = UserConnectionRepository()
    with SessionLocal() as db:
        api_key_service.crud.create(
            db, ApiKeyCreate(key_hash=hash_api_key(api_key), key_prefix=api_key[:KEY_PREFIX_LENGTH], name="rumi")
        )
        for user in _users():
            user_service.crud.create(db, UserCreateInternal(id=uuid.UUID(user["id"]), first_name=user["label"]))
            if garmin_id := user.get("garmin"):
                connections.create(
                    db,
                    UserConnectionCreate(
                        user_id=uuid.UUID(user["id"]),
                        provider="garmin",
                        provider_user_id=garmin_id,
                        access_token="rumi-fixture-garmin-token",
                        refresh_token="rumi-fixture-garmin-refresh",
                        token_expires_at=datetime.now(timezone.utc) + timedelta(days=3650),
                    ),
                )
            if user.get("ultrahuman"):
                connections.create(
                    db,
                    UserConnectionCreate(
                        user_id=uuid.UUID(user["id"]),
                        provider="ultrahuman",
                        provider_user_id=f"uh-{user['label']}",
                        access_token=_ultrahuman_token(user),
                        refresh_token="rumi-fixture-ultrahuman-refresh",
                        token_expires_at=datetime.now(timezone.utc) + timedelta(days=3650),
                        scope="ring_data cgm_data profile",
                    ),
                )
        db.commit()


def _ingest_garmin(client: Any, pushes: list[dict[str, Any]]) -> None:
    from app.utils.webhook_callback import callback_token

    url = f"/api/v1/providers/garmin/webhooks/{callback_token('garmin').get_secret_value()}"
    for push in pushes:
        response = client.post(url, json=push, headers={"garmin-client-id": GARMIN_CLIENT_ID})
        if response.status_code != 200:
            raise SystemExit(f"Garmin push {sorted(push)} answered {response.status_code}: {response.text}")


def _ingest_ultrahuman(user_id: str, today: date, days: int) -> None:
    from app.integrations.celery.tasks.sync_vendor_data_task import sync_vendor_data

    start = datetime.combine(today - timedelta(days=days - 1), time(), tzinfo=timezone.utc)
    end = datetime.combine(today, time(23, 59), tzinfo=timezone.utc)
    sync_vendor_data(user_id, start.isoformat(), end.isoformat(), providers=["ultrahuman"])


def _post_process() -> None:
    """What Celery beat runs after ingestion and the API reads: scores, then the day and session rollup."""
    from app.database import SessionLocal
    from app.integrations import rollup_queue
    from app.integrations.celery.tasks.fill_missing_resilience_scores_task import fill_missing_resilience_scores
    from app.integrations.celery.tasks.fill_missing_sleep_scores_task import fill_missing_sleep_scores
    from app.services.archival_service import archival_service

    fill_missing_sleep_scores()
    fill_missing_resilience_scores()
    with SessionLocal() as db:
        while rollup_queue.has_dirty_days():
            archival_service.roll_up_queued_days(db)
            db.commit()


def seed(today: date, api_key: str) -> list[str]:
    from fastapi.testclient import TestClient

    from app.main import api

    users = _users()
    ultrahuman = {
        _ultrahuman_token({"ultrahuman_zone": zone}): responses
        for zone, responses in _ultrahuman_responses(today).items()
    }
    garmin = {user["id"]: _garmin_pushes(today, user["garmin"]) for user in users if user.get("garmin")}
    fit_files = {url: _fit_file(spec) for _, specs in garmin.values() for url, spec in specs.items()}

    with redirect_stdout(sys.stderr):
        _migrate_and_initialize()
    _create_users_and_key(api_key)
    with _ingestion_patches(_offline_http(ultrahuman, fit_files)) as skipped, TestClient(api) as client:
        for user in users:
            if user["id"] in garmin:
                _ingest_garmin(client, garmin[user["id"]][0])
            if user.get("ultrahuman"):
                _ingest_ultrahuman(user["id"], today, len(ultrahuman[_ultrahuman_token(user)]))
        _post_process()
    return skipped


# ---------------------------------------------------------------------------
# Dump
# ---------------------------------------------------------------------------


def _get_all(client: Any, path: str, params: dict[str, Any]) -> dict[str, Any]:
    """Every page of a GET, cursor or offset paginated, as one record list."""
    pages: list[Any] = []
    cursor: str | None = None
    offset = 0
    while True:
        query = {
            **params,
            **({"cursor": cursor} if cursor else {}),
            **({"offset": offset} if "offset" in params else {}),
        }
        response = client.get(path, params=query)
        body = response.json()
        pages.append(body)
        pagination = body.get("pagination") if isinstance(body, dict) else None
        if response.status_code != 200 or not pagination or not pagination.get("has_more"):
            break
        if "offset" in params:
            offset += len(body["data"])
        else:
            cursor = pagination["next_cursor"]
    if len(pages) == 1 or not all(isinstance(p, dict) and "data" in p for p in pages):
        return {"path": path, "params": params, "status": response.status_code, "body": pages[-1]}
    return {
        "path": path,
        "params": params,
        "status": response.status_code,
        "body": {**pages[0], "data": [record for page in pages for record in page["data"]]},
    }


def dump(base_url: str, api_key: str, today: date, out: Path) -> None:
    """Write the raw JSON of every v1 GET Rumi reads or could read, per fixture user."""
    import httpx

    from app.schemas.enums import SeriesType

    start = (today - timedelta(days=DUMP_DAYS_BEFORE)).isoformat()
    end = (today + timedelta(days=1)).isoformat()
    span = {"start_date": start, "end_date": end, "limit": 100}
    out.mkdir(parents=True, exist_ok=True)
    with httpx.Client(base_url=f"{base_url}/api/v1", headers={"X-Open-Wearables-API-Key": api_key}) as client:
        (out / "oauth_providers.json").write_text(json.dumps(_get_all(client, "/oauth/providers", {}), indent=1))
        for user in _users():
            user_dir = out / user["label"]
            user_dir.mkdir(exist_ok=True)
            base = f"/users/{user['id']}"
            requests: dict[str, tuple[str, dict[str, Any]]] = {
                "connections": (f"{base}/connections", {}),
                "summaries_recovery": (f"{base}/summaries/recovery", span),
                "summaries_sleep": (f"{base}/summaries/sleep", {**span, "filter_by_priority": "false"}),
                "summaries_activity": (f"{base}/summaries/activity", span),
                "summaries_body": (f"{base}/summaries/body", {}),
                "summaries_cycles": (f"{base}/summaries/cycles", span),
                "events_workouts": (f"{base}/events/workouts", {**span, "include": ["zones", "segments"]}),
                "events_sleep": (f"{base}/events/sleep", {**span, "filter_by_priority": "false", "include": "stages"}),
                "events_menstrual_cycles": (f"{base}/events/menstrual-cycles", span),
                "events_activities": (f"{base}/events/activities", span),
                "health_scores": (
                    f"{base}/health-scores",
                    {"start_date": start, "end_date": end, "limit": 100, "offset": 0},
                ),
            }
            for provider in ("garmin", "ultrahuman"):
                requests[f"summaries_recovery_{provider}"] = (
                    f"{base}/summaries/recovery",
                    {**span, "provider": provider},
                )
                requests[f"summaries_activity_{provider}"] = (
                    f"{base}/summaries/activity",
                    {**span, "provider": provider},
                )
            for name, (path, params) in requests.items():
                (user_dir / f"{name}.json").write_text(json.dumps(_get_all(client, path, params), indent=1))
            series: dict[str, Any] = {}
            for series_type in SeriesType:
                for resolution in ("raw", "1day"):
                    params = {
                        "start_time": start,
                        "end_time": end,
                        "types": series_type.value,
                        "resolution": resolution,
                        "limit": 100,
                    }
                    result = _get_all(client, f"{base}/timeseries", params)
                    if result["status"] != 200 or result["body"].get("data"):
                        series[f"{series_type.value}@{resolution}"] = result
            (user_dir / "timeseries.json").write_text(json.dumps(series, indent=1))
    (out / "manifest.json").write_text(
        json.dumps({"today": today.isoformat(), "range": [start, end], "users": _users()}, indent=1)
    )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def _stop(*_: Any) -> None:
    """SIGTERM unwinds like Ctrl-C, so the containers stop; a second signal must not cut that short."""
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    sys.exit(0)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--today", type=date.fromisoformat, required=True, help="Last seeded day, YYYY-MM-DD")
    parser.add_argument("--port", type=int, default=0, help="Port to serve on (default: a free one)")
    parser.add_argument("--api-key", default=DEFAULT_API_KEY, help="API key the server accepts")
    parser.add_argument("--dump-to", type=Path, help="Write every v1 GET response for each user to this directory")
    parser.add_argument("--no-serve", action="store_true", help="Exit after seeding (and the dump)")
    args = parser.parse_args()

    # Deterministic row ids, so a re-run of the same revision serves the same bodies.
    setattr(uuid, "uuid4", _seeded_uuid4(int(args.today.strftime("%Y%m%d"))))
    port = args.port or _free_port()
    signal.signal(signal.SIGTERM, _stop)

    with _containers() as (db_url, redis_host, redis_port):
        _configure_environment(db_url, redis_host, redis_port, port)
        skipped = seed(args.today, args.api_key)

        import threading

        import uvicorn

        from app.main import api

        server = uvicorn.Server(uvicorn.Config(api, host="127.0.0.1", port=port, log_level="warning"))
        thread = threading.Thread(target=server.run, daemon=True)
        thread.start()
        while not server.started:
            thread.join(0.1)
        base_url = f"http://127.0.0.1:{port}"
        if args.dump_to:
            dump(base_url, args.api_key, args.today, args.dump_to.resolve())
        users = [
            {"label": u["label"], "id": u["id"], "providers": [p for p in ("garmin", "ultrahuman") if u.get(p)]}
            for u in _users()
        ]
        print(
            json.dumps(
                {
                    "base_url": f"{base_url}/api/v1",
                    "port": port,
                    "api_key": args.api_key,
                    "today": args.today.isoformat(),
                    "users": users,
                    "skipped_celery_tasks": sorted(set(skipped)),
                }
            ),
            flush=True,
        )
        if args.no_serve:
            server.should_exit = True
        thread.join()


if __name__ == "__main__":
    main()
