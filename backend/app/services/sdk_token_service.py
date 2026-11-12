from datetime import datetime, timedelta, timezone
from uuid import UUID

from jose import jwt

from app.config import settings


def create_sdk_user_token(app_id: str, user_id: str) -> str:
    """Create JWT with SDK scope for a specific user.

    The token is scoped to SDK endpoints only and contains:
    - sub: The user_id (UUID string)
    - scope: "sdk" to identify this as an SDK token
    - app_id: The application ID that created this token
    - exp: Expiration timestamp (configured via access_token_expire_minutes)

    Args:
        app_id: The application ID that requested this token
        user_id: The OpenWearables User ID (UUID string)

    Returns:
        JWT token string
    """
    expire = datetime.now(timezone.utc) + timedelta(minutes=settings.access_token_expire_minutes)
    claims = {
        "sub": user_id,
        "scope": "sdk",
        "app_id": app_id,
        "exp": expire,
    }

    return jwt.encode(claims, settings.secret_key, algorithm=settings.algorithm)


def create_pairing_token(user_id: UUID) -> tuple[str, datetime]:
    """Create a JWT that only lets its holder start a provider OAuth flow for one user.

    It goes into the public pairing link, so it grants nothing else: the authorize
    endpoint is the only place that accepts ``scope="pairing"``.
    """
    expire = datetime.now(timezone.utc) + timedelta(days=settings.pairing_token_expire_days)
    claims = {"sub": str(user_id), "scope": "pairing", "exp": expire}
    return jwt.encode(claims, settings.secret_key, algorithm=settings.algorithm), expire
