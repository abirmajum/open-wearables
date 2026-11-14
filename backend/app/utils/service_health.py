"""Opaque readiness for Redis, Beat, queue consumers and enabled webhook delivery.

Failures considered: Redis unavailable, Beat stopped, a consumer stopped or stuck,
old queued probes arriving after recovery, Svix down, and configuration/exception
details leaking through a public health response. Probe tasks expire; their keys
have a short TTL. No user records or webhook messages are created by these checks.
"""

from functools import lru_cache

import httpx
import redis
from redis.backoff import NoBackoff
from redis.retry import Retry

from app.config import settings
from app.constants.celery_tasks import CeleryQueue

HEARTBEAT_TTL_SECONDS = 120
HEARTBEAT_INTERVAL_SECONDS = 30
HEARTBEAT_KEY_PREFIX = "ow:service-health:queue:"


@lru_cache
def health_redis() -> redis.Redis:
    return redis.from_url(
        settings.redis_url,
        socket_connect_timeout=2,
        socket_timeout=2,
        retry=Retry(NoBackoff(), 0),
        decode_responses=True,
    )


def record_queue_heartbeat(queue: str) -> None:
    queue_name = CeleryQueue(queue)
    health_redis().set(f"{HEARTBEAT_KEY_PREFIX}{queue_name}", "ok", ex=HEARTBEAT_TTL_SECONDS)


def services_are_ready() -> bool:
    """Check recent Beat-to-worker progress and Svix's standard health endpoint."""
    try:
        values = health_redis().mget([f"{HEARTBEAT_KEY_PREFIX}{queue}" for queue in CeleryQueue])
        if not all(value == "ok" for value in values):
            return False
        if settings.outgoing_webhooks_enabled:
            response = httpx.get(f"{settings.svix_server_url.rstrip('/')}/api/v1/health", timeout=2)
            if response.status_code != 200:
                return False
    except Exception:
        return False
    return True
