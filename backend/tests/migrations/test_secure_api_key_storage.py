from collections.abc import Generator
from hashlib import sha256
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from types import ModuleType
from uuid import UUID

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session
from testcontainers.postgres import PostgresContainer

from app.services.api_key_service import api_key_service


@pytest.fixture(autouse=True)
def set_factory_session() -> None:
    return


@pytest.fixture(autouse=True)
def flush_redis() -> None:
    return


@pytest.fixture(scope="session", autouse=True)
def _configure_redis() -> None:
    return


@pytest.fixture(scope="module")
def isolated_engine() -> Generator[Engine, None, None]:
    with PostgresContainer(
        image="postgres:18",
        username="open-wearables",
        password="open-wearables",
        dbname="open_wearables_api_key_migration_test",
        driver="psycopg",
    ) as postgres:
        test_engine = create_engine(postgres.get_connection_url())
        try:
            yield test_engine
        finally:
            test_engine.dispose()


def _load_migration() -> ModuleType:
    path = Path(__file__).parents[2] / "migrations/versions/2026_09_04_1200-a3f4e5d6c7b8_secure_api_key_storage.py"
    spec = spec_from_file_location("secure_api_key_storage", path)
    assert spec is not None
    assert spec.loader is not None
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_legacy_key_survives_secure_storage_migration(isolated_engine: Engine) -> None:
    legacy_secret = "sk-legacy-migration-test-only"
    schema = "api_key_security_migration_test"

    with isolated_engine.connect() as connection:
        transaction = connection.begin()
        try:
            connection.execute(sa.text(f'CREATE SCHEMA "{schema}"'))
            connection.execute(sa.text(f'SET LOCAL search_path TO "{schema}", public'))
            connection.execute(
                sa.text(
                    "CREATE TABLE api_key ("
                    "id VARCHAR(64) PRIMARY KEY, name TEXT NOT NULL, "
                    "created_by UUID NULL, created_at TIMESTAMPTZ NOT NULL DEFAULT now())"
                )
            )
            connection.execute(
                sa.text("INSERT INTO api_key (id, name) VALUES (:secret, 'Legacy')"),
                {"secret": legacy_secret},
            )

            migration = _load_migration()
            migration.op = Operations(MigrationContext.configure(connection))
            migration.upgrade()

            columns = {column["name"]: column for column in sa.inspect(connection).get_columns("api_key")}
            row = connection.execute(sa.text("SELECT id, key_hash, display_prefix FROM api_key")).one()

            assert set(columns) == {
                "id",
                "key_hash",
                "display_prefix",
                "name",
                "created_by",
                "created_at",
            }
            assert isinstance(columns["id"]["type"], sa.UUID)
            assert isinstance(row.id, UUID)
            assert row.key_hash == sha256(legacy_secret.encode("utf-8")).hexdigest()
            assert row.display_prefix == legacy_secret[:11]

            with Session(bind=connection) as session:
                authenticated = api_key_service.validate_api_key(session, legacy_secret)
                assert authenticated.id == row.id
        finally:
            transaction.rollback()
