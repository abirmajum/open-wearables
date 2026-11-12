"""Fixed-window request counters in Redis, shared by every API worker."""

from typing import cast

from app.integrations.redis_client import get_redis_client

# The window starts with the first hit: the expiry is set once, so later hits cannot extend it
_INCR_WITH_WINDOW = (
    "local n = redis.call('incr', KEYS[1]) if n == 1 then redis.call('expire', KEYS[1], ARGV[1]) end return n"
)


def hit(key: str, window_seconds: int) -> int:
    """Count one request against ``key``; the number of requests in its window, this one included."""
    return int(cast(int, get_redis_client().eval(_INCR_WITH_WINDOW, 1, key, window_seconds)))
