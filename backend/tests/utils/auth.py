"""
Authentication helpers for tests.
"""

from datetime import timedelta
from hashlib import sha256
from uuid import UUID

from app.models import ApiKey
from app.utils.security import create_access_token

_TEST_SECRET_NAMESPACE = b"open-wearables-test-api-key:"


def developer_auth_headers(developer_id: UUID | str) -> dict[str, str]:
    """Generate JWT Bearer authorization headers for a developer."""
    token = create_access_token(subject=str(developer_id))
    return {"Authorization": f"Bearer {token}"}


def api_key_secret_for_id(api_key_id: UUID) -> str:
    digest = sha256(_TEST_SECRET_NAMESPACE + api_key_id.bytes).hexdigest()
    return f"test-{digest}"


def api_key_secret(api_key: ApiKey) -> str:
    return api_key_secret_for_id(api_key.id)


def api_key_headers(api_key: ApiKey | str) -> dict[str, str]:
    secret = api_key_secret(api_key) if isinstance(api_key, ApiKey) else api_key
    return {"X-Open-Wearables-API-Key": secret}


def create_test_token(
    developer_id: UUID | str,
    expires_delta: timedelta | None = None,
) -> str:
    """Create a custom JWT token for testing."""
    return create_access_token(subject=str(developer_id), expires_delta=expires_delta)
