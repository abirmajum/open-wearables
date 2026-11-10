"""Rehearse the fork -> pinned upstream migration chain on disposable PostgreSQL.

Failure cases: divergent Alembic heads, replay failures, lost opaque API-key IDs,
changed credential bytes, account deletion cascading across cycle ownership, and
for the 1.0 upgrade: wearable samples lost in the data_point_series -> series_sample
rename, health scores lost when their unique key gains data_source_id, and WHOOP
cycle revisions lost across the history merge.
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
HEAD = "63c70566c6d3"
# The fork head before upstream 0.10.0, and the fork head Railway runs before 1.0.
FORK_0_10_BASE = "a3f4e5d6c7b8"
FORK_1_0_BASE = "f82e01ac749d"


def main() -> None:
    output = Path(sys.argv[1] if len(sys.argv) > 1 else "/tmp/ow-fork-upgrade-report.json")
    image = sys.argv[2] if len(sys.argv) > 2 else "postgres:17"
    if image not in {"postgres:17", "postgres:18"}:
        raise ValueError("Use a supported disposable PostgreSQL fixture image")
    results: list[dict[str, object]] = []
    for mode in ("fresh", "fork-0.10", "fork-1.0"):
        with PostgresContainer(image, username="fixture", password="fixture", dbname="fixture", driver="psycopg") as pg:
            env = {
                **os.environ,
                "DB_HOST": pg.get_container_host_ip(),
                "DB_PORT": str(pg.get_exposed_port(5432)),
                "DB_USER": "fixture",
                "DB_PASSWORD": "fixture",
                "DB_NAME": "fixture",
                "TELEMETRY_ENABLED": "false",
                "SECRET_KEY": "fictional-upgrade-only-secret-key-of-32-plus-chars",
                "MASTER_KEY": "dGVzdC1tYXN0ZXIta2V5LWZvci10ZXN0aW5nLW9ubHk=",
                "OW_IGNORE_ENV_FILE": "1",
            }
            engine = create_engine(pg.get_connection_url())
            user_id, key_id, connection_id, source_id = uuid4(), uuid4(), uuid4(), uuid4()
            sleep_id, steps_id, heart_rate_id, recovery_id, sleep_score_id = (uuid4() for _ in range(5))
            secret = "sk-fictional-upgrade-preserved-key"
            digest = hashlib.sha256(secret.encode()).hexdigest()

            def run(*args: str) -> None:
                result = subprocess.run([sys.executable, *args], cwd=BACKEND, env=env, capture_output=True, text=True)
                if result.returncode:
                    raise RuntimeError(f"{mode} {' '.join(args)} failed:\n{result.stderr}")

            def upgrade(target: str) -> None:
                run("-m", "alembic", "upgrade", target)

            def seed_auth(db, prefix_column: str) -> None:  # noqa: ANN001
                db.execute(text("INSERT INTO \"user\" (id,first_name) VALUES (:id,'Fixture')"), {"id": user_id})
                db.execute(
                    text(f"INSERT INTO api_key (id,key_hash,{prefix_column},name) VALUES (:id,:hash,:prefix,'Key')"),
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

            try:
                if mode == "fork-0.10":
                    upgrade(FORK_0_10_BASE)
                    with engine.begin() as db:
                        seed_auth(db, "display_prefix")
                if mode == "fork-1.0":
                    upgrade(FORK_1_0_BASE)
                    with engine.begin() as db:
                        seed_auth(db, "key_prefix")
                        # The app seeds series types at startup, not in a migration.
                        db.execute(
                            text("""INSERT INTO series_type_definition (id,code,unit)
                            VALUES (1,'heart_rate','bpm'),(80,'steps','count')""")
                        )
                        db.execute(
                            text("""INSERT INTO data_source (id,user_id,provider,user_connection_id,source)
                            VALUES (:id,:user,'whoop',:conn,'whoop')"""),
                            {"id": source_id, "user": user_id, "conn": connection_id},
                        )
                        db.execute(
                            text("""INSERT INTO event_record
                            (id,external_id,data_source_id,category,type,source_name,duration_seconds,
                                start_datetime,end_datetime,zone_offset)
                            VALUES (:id,'fictional-sleep',:source,'sleep','sleep_session','Whoop',28800,
                                '2026-11-01T22:00:00+00','2026-11-02T06:00:00+00','+03:00')"""),
                            {"id": sleep_id, "source": source_id},
                        )
                        db.execute(
                            text("""INSERT INTO data_point_series
                            (id,data_source_id,recorded_at,zone_offset,value,series_type_definition_id,is_daily_total)
                            VALUES
                            (:steps,:source,'2026-11-01T21:00:00+00','+03:00',8123,
                                80,true),
                            (:hr,:source,'2026-11-02T03:00:00+00','+03:00',52.5,
                                1,false)"""),
                            {"steps": steps_id, "hr": heart_rate_id, "source": source_id},
                        )
                        db.execute(
                            text("""INSERT INTO health_score
                            (id,user_id,provider,category,value,recorded_at,zone_offset,event_record_id)
                            VALUES
                            (:recovery,:user,'whoop','recovery',67,'2026-11-02T06:30:00+00','+03:00',NULL),
                            (:sleep_score,:user,'whoop','sleep',88,'2026-11-01T22:00:00+00','+03:00',:sleep)"""),
                            dict(recovery=recovery_id, sleep_score=sleep_score_id, user=user_id, sleep=sleep_id),
                        )
                        db.execute(
                            text("""INSERT INTO provider_cycle (id,user_id,provider,updated_at,fetched_at,payload)
                            VALUES ('9001',:user,'whoop',now(),now(),'{}')"""),
                            {"user": user_id},
                        )
                upgrade("head")
                upgrade("head")
                with engine.begin() as db:
                    heads = list(db.scalars(text("SELECT version_num FROM alembic_version")))
                    assert heads == [HEAD], heads
                    assert db.scalar(text("SELECT to_regclass('provider_cycle')")) == "provider_cycle"
                    assert db.scalar(text("SELECT to_regclass('data_point_series')")) is None
                    assert db.scalar(text("SELECT to_regclass('series_aggregate')")) == "series_aggregate"
                    constraints = set(db.scalars(text("SELECT conname FROM pg_constraint")))
                    assert "uq_health_score_user_provider_source_category_time" in constraints
                    assert "uq_health_score_user_provider_category_time" not in constraints
                    if mode != "fresh":
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
                    if mode == "fork-1.0":
                        samples = dict(
                            db.execute(text("SELECT id, value FROM series_sample ORDER BY recorded_at")).all()
                        )
                        assert {k: float(v) for k, v in samples.items()} == {steps_id: 8123.0, heart_rate_id: 52.5}
                        scores = dict(db.execute(text("SELECT id, event_record_id FROM health_score")).all())
                        assert scores == {recovery_id: None, sleep_score_id: sleep_id}, scores
                        assert db.scalar(text("SELECT count(*) FROM provider_cycle")) == 1
                if mode == "fork-1.0":
                    # Ingestion now records WHOOP sleep scores at the wake-up; startup moves the stored ones there.
                    run("scripts/data_migrations/restamp_provider_sleep_scores.py")
                    with engine.begin() as db:
                        restamped = db.execute(
                            text("SELECT recorded_at, zone_offset, event_record_id FROM health_score WHERE id=:id"),
                            {"id": sleep_score_id},
                        ).one()
                        assert restamped.recorded_at.isoformat() == "2026-11-02T06:00:00+00:00", restamped
                        assert restamped.zone_offset == "+03:00"
                        assert restamped.event_record_id == sleep_id
                with engine.begin() as db:
                    if mode == "fork-0.10":
                        db.execute(
                            text("""INSERT INTO provider_cycle (id,user_id,provider,updated_at,fetched_at,payload)
                            VALUES ('9001',:user,'whoop',now(),now(),'{}')"""),
                            {"user": user_id},
                        )
                    if mode != "fresh":
                        db.execute(text('DELETE FROM "user" WHERE id=:user'), {"user": user_id})
                        assert db.scalar(text("SELECT count(*) FROM provider_cycle")) == 0
                        assert db.scalar(text("SELECT count(*) FROM series_sample")) == 0
                results.append(
                    {"mode": mode, "passed": True, "heads": heads, "idempotent": True, "preservedAuth": mode != "fresh"}
                )
            finally:
                engine.dispose()
    output.parent.mkdir(parents=True, exist_ok=True)
    revision = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=BACKEND, text=True).strip()
    output.write_text(
        json.dumps(
            {"revision": revision, "postgresImage": image, "fictionalData": True, "passed": True, "results": results},
            indent=2,
        )
    )
    print(output)


if __name__ == "__main__":
    main()
