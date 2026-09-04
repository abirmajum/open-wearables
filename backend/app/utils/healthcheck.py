import logging

from fastapi import APIRouter
from sqlalchemy import text

from app.database import DbSession, engine
from app.utils.structured_logging import log_structured

healthcheck_router = APIRouter()
logger = logging.getLogger(__name__)


def database_is_ready() -> bool:
    """Return whether the API can obtain and use a database connection.

    Keep the failure deliberately opaque: connection exceptions can contain hostnames,
    usernames, or driver-specific details and this check is safe to expose publicly.
    """
    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
    except Exception:
        log_structured(logger, "warning", "Database readiness check failed", action="database_readiness_failed")
        return False
    return True


def get_pool_status() -> dict[str, str]:
    """Get connection pool status for monitoring."""
    pool = engine.pool
    return {
        "max_pool_size": str(pool.size()),  # ty:ignore[unresolved-attribute]
        "connections_ready_for_reuse": str(pool.checkedin()),  # ty:ignore[unresolved-attribute]
        "active_connections": str(pool.checkedout()),  # ty:ignore[unresolved-attribute]
        "overflow": str(pool.overflow()),  # ty:ignore[unresolved-attribute]
    }


@healthcheck_router.get("/db")
async def database_health(db: DbSession) -> dict[str, str | dict[str, str]]:
    """Database health check endpoint."""
    try:
        # Test connection
        db.execute(text("SELECT 1"))

        pool_status = get_pool_status()
        return {
            "status": "healthy",
            "pool": pool_status,
        }
    except Exception:
        log_structured(logger, "warning", "Database health check failed", action="database_health_check_failed")
        return {
            "status": "unhealthy",
        }
