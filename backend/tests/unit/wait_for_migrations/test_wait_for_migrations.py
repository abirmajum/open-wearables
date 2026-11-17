"""The worker's wait for migrations, against a throwaway SQLite database holding only an alembic_version table."""

import importlib.util
import threading
import time
from pathlib import Path
from types import ModuleType

import pytest
from sqlalchemy import create_engine, text

SCRIPT = Path(__file__).resolve().parents[3] / "scripts/start/wait_for_migrations.py"


@pytest.fixture
def wait() -> ModuleType:
    spec = importlib.util.spec_from_file_location("wait_for_migrations", SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def stamp(db_uri: str, revision: str) -> None:
    engine = create_engine(db_uri)
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE IF NOT EXISTS alembic_version (version_num VARCHAR(32) PRIMARY KEY)"))
        connection.execute(text("DELETE FROM alembic_version"))
        connection.execute(text("INSERT INTO alembic_version VALUES (:revision)"), {"revision": revision})
    engine.dispose()


def test_a_database_at_head_starts_the_workers_at_once(wait: ModuleType, tmp_path: Path) -> None:
    db_uri = f"sqlite:///{tmp_path / 'db.sqlite'}"
    stamp(db_uri, "head_rev")

    started = time.monotonic()
    assert wait.wait_for_head(db_uri, {"head_rev"}, timeout=5, poll_interval=1) == 0
    assert time.monotonic() - started < 1


def test_a_database_behind_head_fails_once_the_wait_runs_out(wait: ModuleType, tmp_path: Path) -> None:
    db_uri = f"sqlite:///{tmp_path / 'db.sqlite'}"
    stamp(db_uri, "old_rev")

    assert wait.wait_for_head(db_uri, {"head_rev"}, timeout=0.3, poll_interval=0.05) == 1


def test_the_workers_start_once_the_migration_lands(wait: ModuleType, tmp_path: Path) -> None:
    db_uri = f"sqlite:///{tmp_path / 'db.sqlite'}"
    stamp(db_uri, "old_rev")
    migration = threading.Timer(0.3, stamp, args=(db_uri, "head_rev"))
    migration.start()
    try:
        assert wait.wait_for_head(db_uri, {"head_rev"}, timeout=5, poll_interval=0.05) == 0
    finally:
        migration.cancel()


def test_a_database_ahead_of_this_code_fails_at_once_naming_the_revision(
    wait: ModuleType, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    db_uri = f"sqlite:///{tmp_path / 'db.sqlite'}"
    stamp(db_uri, "newer_rev")

    started = time.monotonic()
    assert wait.wait_for_head(db_uri, {"head_rev"}, timeout=5, poll_interval=1, known={"old_rev", "head_rev"}) == 2
    assert time.monotonic() - started < 1
    output = capsys.readouterr().out
    assert "newer_rev" in output
    assert "deploy the newer code or downgrade" in output


def test_a_database_behind_head_on_a_known_revision_is_waited_for(wait: ModuleType, tmp_path: Path) -> None:
    db_uri = f"sqlite:///{tmp_path / 'db.sqlite'}"
    stamp(db_uri, "old_rev")

    assert wait.wait_for_head(db_uri, {"head_rev"}, timeout=0.3, poll_interval=0.05, known={"old_rev", "head_rev"}) == 1


def test_the_known_revisions_include_the_heads(wait: ModuleType) -> None:
    assert wait.code_heads() <= wait.code_revisions()


def test_an_unreachable_database_is_waited_for_then_fails(wait: ModuleType, tmp_path: Path) -> None:
    db_uri = f"sqlite:///{tmp_path / 'missing' / 'db.sqlite'}"

    assert wait.wait_for_head(db_uri, {"head_rev"}, timeout=0.2, poll_interval=0.05) == 1


def test_the_code_heads_name_one_migration_file(wait: ModuleType) -> None:
    (head,) = wait.code_heads()

    assert any(head in path.name for path in (SCRIPT.parents[2] / "migrations" / "versions").iterdir())
