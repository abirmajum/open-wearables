from unittest.mock import patch

import pytest

from app.constants.celery_tasks import CeleryQueue
from app.utils.service_health import record_queue_heartbeat, service_database_is_ready, services_are_ready


@pytest.mark.parametrize("values", [["ok"] * len(CeleryQueue), [None] * len(CeleryQueue), ["invalid"]])
def test_queue_readiness(values: list[str | None]) -> None:
    with (
        patch("app.utils.service_health.health_redis") as redis,
        patch("app.utils.service_health.settings") as settings,
    ):
        settings.outgoing_webhooks_enabled = False
        redis.return_value.mget.return_value = values
        assert services_are_ready() is all(value == "ok" for value in values)


def test_redis_failure_is_opaque() -> None:
    with patch("app.utils.service_health.health_redis", side_effect=RuntimeError("secret")):
        assert services_are_ready() is False


@pytest.mark.parametrize("code", [200, 503])
def test_enabled_svix_health(code: int) -> None:
    with (
        patch("app.utils.service_health.health_redis") as redis,
        patch("app.utils.service_health.settings") as settings,
        patch("app.utils.service_health.httpx.stream") as stream,
    ):
        settings.svix_server_url = "http://svix"
        settings.outgoing_webhooks_enabled = True
        redis.return_value.mget.return_value = ["ok"] * len(CeleryQueue)
        stream.return_value.__enter__.return_value.status_code = code
        assert services_are_ready() is (code == 200)
        stream.assert_called_once_with("GET", "http://svix/api/v1/health", timeout=2)


def test_heartbeat_expiry_and_queue_validation() -> None:
    with patch("app.utils.service_health.health_redis") as redis:
        record_queue_heartbeat("default")
        redis.return_value.set.assert_called_once_with("ow:service-health:queue:default", "ok", ex=120)
        with pytest.raises(ValueError, match="unknown"):
            record_queue_heartbeat("unknown")


def test_database_failure_is_opaque() -> None:
    with patch("app.utils.service_health.health_database", side_effect=RuntimeError("secret")):
        assert service_database_is_ready() is False


def test_database_probe_executes_query() -> None:
    with patch("app.utils.service_health.health_database") as database:
        assert service_database_is_ready() is True
        database.return_value.connect.return_value.__enter__.return_value.execute.assert_called_once()
