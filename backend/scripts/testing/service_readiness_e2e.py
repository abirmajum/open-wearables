"""Exercise service readiness through HTTP with disposable Docker DB/Redis and real Celery.

Run from backend: uv run python scripts/testing/service_readiness_e2e.py OUTPUT_DIR
Svix is a local HTTP fixture; no vendor, real user, Sentry or Railway is contacted.
Only this run's containers/processes are stopped. report.json is the repeatable receipt.
"""

import json
import os
import socket
import subprocess
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import httpx
import redis


def port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def main() -> None:
    output = Path(sys.argv[1]).resolve()
    output.mkdir(parents=True, exist_ok=False)
    run_id = uuid.uuid4().hex[:10]
    database_name, redis_name = f"ow-health-db-{run_id}", f"ow-health-redis-{run_id}"
    db_port, redis_port, api_port, svix_port = port(), port(), port(), port()
    reports: list[dict[str, Any]] = []
    processes: dict[str, subprocess.Popen[bytes]] = {}
    created: list[str] = []
    svix_up = True

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            self.send_response(200 if svix_up and self.path == "/api/v1/health" else 503)
            self.end_headers()

        def log_message(self, format: str, *args: Any) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", svix_port), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    env = {
        **os.environ,
        "SECRET_KEY": "service-readiness-fixture-secret-0000000000",
        "MASTER_KEY": "dGVzdC1tYXN0ZXIta2V5LWZvci10ZXN0aW5nLW9ubHk=",
        "DB_HOST": "127.0.0.1",
        "DB_PORT": str(db_port),
        "DB_NAME": "health",
        "DB_USER": "health",
        "DB_PASSWORD": "fixture",
        "REDIS_HOST": "127.0.0.1",
        "REDIS_PORT": str(redis_port),
        "REDIS_DB": "0",
        "REDIS_PASSWORD": "",
        "REDIS_USERNAME": "",
        "REDIS_SSL": "false",
        "SENTRY_ENABLED": "false",
        "OTEL_ENABLED": "false",
        "OTEL_SDK_DISABLED": "true",
        "TELEMETRY_ENABLED": "false",
        "OUTGOING_WEBHOOKS_ENABLED": "true",
        "SVIX_SERVER_URL": f"http://127.0.0.1:{svix_port}",
        "PYTHONPATH": f"{output}:{Path.cwd()}",
    }
    # Keep the real production Beat heartbeat schedule, without business jobs.
    (output / "fixture_celery.py").write_text(
        "from app.main import celery_app\n"
        "import sys\n"
        "if sys.platform != 'linux':\n"
        "    celery_app.conf.broker_transport_options.pop('socket_keepalive_options', None)\n"
        "celery_app.conf.beat_schedule = {k:v for k,v in celery_app.conf.beat_schedule.items() "
        "if k.startswith('service-heartbeat-')}\n"
    )
    client = redis.Redis(host="127.0.0.1", port=redis_port, socket_timeout=2, decode_responses=True)
    celery = [sys.executable, "-m", "celery", "-A", "fixture_celery:celery_app"]
    commands = {
        "api": [sys.executable, "-m", "uvicorn", "app.main:api", "--port", str(api_port), "--lifespan", "off"],
        "beat": [*celery, "beat", "--schedule", str(output / "beat.db"), "--pidfile", str(output / "beat.pid")],
        "io": [*celery, "worker", "-P", "solo", "-n", "io@fixture", "-Q", "default,sdk_sync,garmin_sync,webhook_sync"],
        "cpu": [*celery, "worker", "-P", "solo", "-n", "cpu@fixture", "-Q", "xml_sync"],
    }

    def docker(*args: str) -> str:
        return subprocess.check_output(["docker", *args], text=True, timeout=40).strip()

    def start(name: str) -> None:
        with (output / f"{name}.log").open("ab") as log:
            processes[name] = subprocess.Popen(commands[name], env=env, stdout=log, stderr=log)

    def stop(name: str) -> None:
        process = processes.pop(name)
        process.terminate()
        try:
            process.wait(timeout=15)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()

    def expect(name: str, code: int) -> None:
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            try:
                response = httpx.get(f"http://127.0.0.1:{api_port}/services-readyz", timeout=5)
                expected = {"status": "ready" if code == 200 else "not_ready"}
                if response.status_code == code and response.json() == expected:
                    assert response.headers["cache-control"] == "no-store"
                    reports.append({"case": name, "status": code, "body": response.json()})
                    print(json.dumps(reports[-1]), flush=True)
                    return
            except httpx.HTTPError:
                pass
            time.sleep(1)
        raise AssertionError(f"{name}: expected {code}")

    try:
        docker(
            "run",
            "-d",
            "--name",
            database_name,
            "-p",
            f"127.0.0.1:{db_port}:5432",
            "-e",
            "POSTGRES_DB=health",
            "-e",
            "POSTGRES_USER=health",
            "-e",
            "POSTGRES_PASSWORD=fixture",
            "postgres:17-alpine",
        )
        created.append(database_name)
        docker("run", "-d", "--name", redis_name, "-p", f"127.0.0.1:{redis_port}:6379", "redis:7-alpine")
        created.append(redis_name)
        for name in commands:
            start(name)
        expect("all_services_healthy", 200)
        stop("cpu")
        # Advance only a fixture key's expiry; production TTL stays 120 seconds.
        client.expire("ow:service-health:queue:xml_sync", 1)
        time.sleep(2)
        expect("cpu_consumer_stopped", 503)
        start("cpu")
        expect("cpu_consumer_recovered", 200)
        stop("beat")
        for key in client.scan_iter("ow:service-health:queue:*"):
            client.expire(key, 1)
        time.sleep(2)
        expect("beat_stopped", 503)
        start("beat")
        expect("beat_recovered", 200)
        svix_up = False
        expect("svix_down", 503)
        svix_up = True
        expect("svix_recovered", 200)
        docker("stop", redis_name)
        expect("redis_down", 503)
        docker("start", redis_name)
        expect("redis_recovered", 200)
        docker("stop", database_name)
        expect("database_down", 503)
        docker("start", database_name)
        expect("database_recovered", 200)
        receipt = {"passed": True, "cases": reports, "limits": ["Svix HTTP fixture", "accelerated fixture key expiry"]}
    finally:
        for name in list(processes):
            stop(name)
        for name in created:
            docker("rm", "-f", name)
        server.shutdown()
        (output / "report.json").write_text(
            json.dumps(locals().get("receipt", {"passed": False, "cases": reports}), indent=2)
        )


if __name__ == "__main__":
    main()
