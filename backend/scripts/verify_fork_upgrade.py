"""Rehearse the fork -> pinned upstream migration chain on disposable PostgreSQL.

Failure cases: divergent Alembic heads, replay failures, lost opaque API-key IDs,
changed credential bytes, account deletion cascading across cycle ownership.
Never accepts an existing database URL. Data-correction scripts have their own
fixture regressions; this rehearsal covers schema evolution and auth continuity.
"""

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path
from uuid import uuid4

from sqlalchemy import create_engine, text
from testcontainers.postgres import PostgresContainer

BACKEND = Path(__file__).resolve().parents[1]


def main() -> None:
    output = Path(sys.argv[1] if len(sys.argv) > 1 else "/tmp/ow-fork-upgrade-report.json")
    results: list[dict[str, object]] = []
    for mode in ("fresh", "fork"):
        with PostgresContainer(
            "postgres:18", username="fixture", password="fixture", dbname="fixture", driver="psycopg"
        ) as pg:
            env = {
                **os.environ,
                "DB_HOST": pg.get_container_host_ip(),
                "DB_PORT": str(pg.get_exposed_port(5432)),
                "DB_USER": "fixture",
                "DB_PASSWORD": "fixture",
                "DB_NAME": "fixture",
                "TELEMETRY_ENABLED": "false",
                "SECRET_KEY": "fictional-upgrade-only-secret",
                "MASTER_KEY": "dGVzdC1tYXN0ZXIta2V5LWZvci10ZXN0aW5nLW9ubHk=",
                "OW_IGNORE_ENV_FILE": "1",
            }
            engine = create_engine(pg.get_connection_url())
            user_id, key_id, connection_id = uuid4(), uuid4(), uuid4()
            secret = "sk-fictional-upgrade-preserved-key"
            digest = hashlib.sha256(secret.encode()).hexdigest()

            def upgrade(target: str) -> None:
                result = subprocess.run(
                    [sys.executable, "-m", "alembic", "upgrade", target],
                    cwd=BACKEND,
                    env=env,
                    capture_output=True,
                    text=True,
                )
                if result.returncode:
                    raise RuntimeError(f"{mode} upgrade {target} failed:\n{result.stderr}")

            try:
                if mode == "fork":
                    upgrade("a3f4e5d6c7b8")
                    with engine.begin() as db:
                        db.execute(text("INSERT INTO \"user\" (id,first_name) VALUES (:id,'Fixture')"), {"id": user_id})
                        db.execute(
                            text(
                                "INSERT INTO api_key (id,key_hash,display_prefix,name) "
                                "VALUES (:id,:hash,:prefix,'Fixture')"
                            ),
                            {"id": key_id, "hash": digest, "prefix": secret[:11]},
                        )
                        db.execute(
                            text("""INSERT INTO user_connection
                            (id,user_id,provider,provider_user_id,access_token,
                                refresh_token,status,created_at,updated_at)
                            VALUES (:id,:user,'whoop','fictional-account','fictional-token',
                                'fictional-refresh','active',now(),now())"""),
                            {"id": connection_id, "user": user_id},
                        )
                upgrade("head")
                upgrade("head")
                with engine.begin() as db:
                    heads = list(db.scalars(text("SELECT version_num FROM alembic_version")))
                    assert heads == ["f82e01ac749d"], heads
                    assert db.scalar(text("SELECT to_regclass('provider_cycle')")) == "provider_cycle"
                    if mode == "fork":
                        row = db.execute(text("SELECT id,key_hash,key_prefix FROM api_key")).one()
                        assert row.id == key_id
                        assert row.key_hash == digest
                        assert row.key_prefix == secret[:11]
                        connection = db.execute(
                            text("SELECT id,user_id,access_token,refresh_token FROM user_connection")
                        ).one()
                        assert connection.id == connection_id
                        assert connection.user_id == user_id
                        assert connection.access_token == "fictional-token"
                        assert connection.refresh_token == "fictional-refresh"
                        db.execute(
                            text("""INSERT INTO provider_cycle (id,user_id,provider,updated_at,fetched_at,payload)
                            VALUES ('9001',:user,'whoop',now(),now(),'{}')"""),
                            {"user": user_id},
                        )
                        db.execute(text('DELETE FROM "user" WHERE id=:user'), {"user": user_id})
                        assert db.scalar(text("SELECT count(*) FROM provider_cycle")) == 0
                results.append(
                    {"mode": mode, "passed": True, "heads": heads, "idempotent": True, "preservedAuth": mode == "fork"}
                )
            finally:
                engine.dispose()
    output.parent.mkdir(parents=True, exist_ok=True)
    revision = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=BACKEND, text=True).strip()
    output.write_text(
        json.dumps({"revision": revision, "fictionalData": True, "passed": True, "results": results}, indent=2)
    )
    print(output)


if __name__ == "__main__":
    main()
