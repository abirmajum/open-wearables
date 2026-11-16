"""Wait until the database is at the Alembic head before a worker starts consuming.

The API container applies migrations at start and the worker does not, so a worker on new code could otherwise
query columns that do not exist yet and fail its tasks until their retries run out.
"""

import sys
import time
from pathlib import Path

from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine

TIMEOUT_SECONDS = 600.0
POLL_INTERVAL_SECONDS = 5.0
ALEMBIC_INI = Path(__file__).resolve().parents[2] / "alembic.ini"


def code_heads() -> set[str]:
    config = Config(str(ALEMBIC_INI))
    config.set_main_option("script_location", str(ALEMBIC_INI.parent / "migrations"))
    return set(ScriptDirectory.from_config(config).get_heads())


def wait_for_head(db_uri: str, heads: set[str], timeout: float, poll_interval: float) -> int:
    """0 once the database is at ``heads``; 1 if it is not by ``timeout``, so the platform restarts the worker."""
    engine = create_engine(db_uri, pool_pre_ping=True)
    deadline = time.monotonic() + timeout
    try:
        while True:
            try:
                with engine.connect() as connection:
                    current: set[str] | str = set(MigrationContext.configure(connection).get_current_heads())
            except Exception as e:
                current = f"unreachable ({type(e).__name__})"
            if current == heads:
                print(f"Database at Alembic head {sorted(heads)}; starting workers", flush=True)
                return 0
            if time.monotonic() >= deadline:
                print(
                    f"Database still at {current}, not Alembic head {sorted(heads)}, after {timeout:.0f}s; exiting so "
                    "the worker is restarted",
                    flush=True,
                )
                return 1
            print(f"Waiting for migrations: database at {current}, Alembic head {sorted(heads)}", flush=True)
            time.sleep(poll_interval)
    finally:
        engine.dispose()


def main() -> int:
    from app.config import settings

    return wait_for_head(settings.db_uri, code_heads(), TIMEOUT_SECONDS, POLL_INTERVAL_SECONDS)


if __name__ == "__main__":
    sys.exit(main())
