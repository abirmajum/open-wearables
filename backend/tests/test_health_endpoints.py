"""Contract tests for process liveness and dependency readiness endpoints."""

import inspect
from unittest.mock import Mock, patch

import pytest
from fastapi.testclient import TestClient

from app.main import readiness


def test_readiness_handler_is_sync_for_fastapi_threadpool_execution() -> None:
    """A sync path operation keeps its blocking SQLAlchemy call off the event loop."""
    assert inspect.iscoroutinefunction(readiness) is False


def test_liveness_reports_only_process_status(client: TestClient) -> None:
    response = client.get("/healthz")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


@patch("app.main.database_is_ready", return_value=True)
def test_readiness_reports_ready_when_database_is_usable(mock_database_is_ready: Mock, client: TestClient) -> None:
    response = client.get("/readyz")

    assert response.status_code == 200
    assert response.json() == {"status": "ready"}
    mock_database_is_ready.assert_called_once_with()


@pytest.mark.parametrize(("database", "services"), [(True, True), (False, True), (True, False)])
def test_service_readiness_is_opaque(database: bool, services: bool, client: TestClient) -> None:
    with (
        patch("app.main.service_database_is_ready", return_value=database),
        patch("app.main.services_are_ready", return_value=services),
    ):
        response = client.get("/services-readyz")
    assert response.status_code == (200 if database and services else 503)
    assert response.json() == {"status": "ready" if database and services else "not_ready"}
    assert response.headers["cache-control"] == "no-store"


@patch("app.main.database_is_ready", return_value=False)
def test_readiness_is_sanitized_and_unavailable_when_database_is_down(
    mock_database_is_ready: Mock, client: TestClient
) -> None:
    response = client.get("/readyz")

    assert response.status_code == 503
    assert response.json() == {"status": "not_ready"}
    mock_database_is_ready.assert_called_once_with()
