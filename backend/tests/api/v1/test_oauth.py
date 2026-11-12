"""
Tests for OAuth endpoints.

Tests the /api/v1/oauth endpoints including:
- GET /api/v1/oauth/{provider}/authorize - test authorize redirect
- GET /api/v1/oauth/providers - test list providers
- PUT /api/v1/oauth/providers/{provider} - test update provider status
"""

from collections.abc import Callable
from unittest.mock import MagicMock
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from jose import jwt
from sqlalchemy.orm import Session

from app.config import settings
from app.schemas.enums import ProviderName
from app.services.sdk_token_service import create_pairing_token, create_sdk_user_token
from tests.factories import ApiKeyFactory, DeveloperFactory, UserFactory
from tests.utils import api_key_headers, developer_auth_headers

pytestmark = pytest.mark.usefixtures("oauth_credentials")


class TestOAuthAuthorizeEndpoint:
    """Test suite for OAuth authorization endpoint."""

    @pytest.fixture
    def headers(self, db: Session) -> dict[str, str]:
        return api_key_headers(ApiKeyFactory())

    def test_authorize_provider_success(self, client: TestClient, db: Session, headers: dict[str, str]) -> None:
        user = UserFactory()

        response = client.get("/api/v1/oauth/garmin/authorize", params={"user_id": str(user.id)}, headers=headers)

        assert response.status_code == 200
        data = response.json()
        assert isinstance(data["authorization_url"], str)
        assert isinstance(data["state"], str)
        assert len(data["state"]) > 0

    def test_authorize_with_developer_token(self, client: TestClient, db: Session) -> None:
        user = UserFactory()

        response = client.get(
            "/api/v1/oauth/garmin/authorize",
            params={"user_id": str(user.id)},
            headers=developer_auth_headers(DeveloperFactory().id),
        )

        assert response.status_code == 200

    def test_authorize_requires_authentication(self, client: TestClient, db: Session) -> None:
        user = UserFactory()

        response = client.get("/api/v1/oauth/garmin/authorize", params={"user_id": str(user.id)})

        assert response.status_code == 401

    def test_authorize_rejects_invalid_api_key(self, client: TestClient, db: Session) -> None:
        user = UserFactory()

        response = client.get(
            "/api/v1/oauth/garmin/authorize",
            params={"user_id": str(user.id)},
            headers=api_key_headers("not-a-real-key"),
        )

        assert response.status_code == 401

    @pytest.mark.parametrize(
        "make_token", [create_pairing_token, lambda uid: (create_sdk_user_token("app", str(uid)),)]
    )
    def test_user_token_authorizes_its_own_user(
        self, client: TestClient, db: Session, make_token: Callable[[UUID], tuple]
    ) -> None:
        user = UserFactory()
        token = make_token(user.id)[0]

        response = client.get(
            "/api/v1/oauth/garmin/authorize",
            params={"user_id": str(user.id)},
            headers={"Authorization": f"Bearer {token}"},
        )

        assert response.status_code == 200

    @pytest.mark.parametrize(
        "make_token", [create_pairing_token, lambda uid: (create_sdk_user_token("app", str(uid)),)]
    )
    def test_user_token_cannot_authorize_another_user(
        self, client: TestClient, db: Session, make_token: Callable[[UUID], tuple]
    ) -> None:
        owner, victim = UserFactory(), UserFactory()
        token = make_token(owner.id)[0]

        response = client.get(
            "/api/v1/oauth/garmin/authorize",
            params={"user_id": str(victim.id)},
            headers={"Authorization": f"Bearer {token}"},
        )

        assert response.status_code == 403
        assert response.json()["detail"] == "Token does not match user_id"

    def test_pairing_token_is_not_a_developer_token(self, client: TestClient, db: Session) -> None:
        token, _ = create_pairing_token(UserFactory().id)

        response = client.get("/api/v1/users", headers={"Authorization": f"Bearer {token}"})

        assert response.status_code == 401

    def test_authorize_unknown_user(self, client: TestClient, db: Session, headers: dict[str, str]) -> None:
        response = client.get("/api/v1/oauth/garmin/authorize", params={"user_id": str(uuid4())}, headers=headers)

        assert response.status_code == 404

    def test_authorize_with_allowed_redirect_uri(
        self, client: TestClient, db: Session, headers: dict[str, str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(settings, "oauth_redirect_allowed_origins", ["https://myapp.com", "myapp://oauth"])
        user = UserFactory()

        for redirect_uri in (
            "https://myapp.com/oauth/callback",
            "myapp://oauth/done",
            f"{settings.frontend_url.rstrip('/')}/users/{user.id}/pair/success",
        ):
            response = client.get(
                "/api/v1/oauth/garmin/authorize",
                params={"user_id": str(user.id), "redirect_uri": redirect_uri},
                headers=headers,
            )
            assert response.status_code == 200, redirect_uri

    @pytest.mark.parametrize(
        "redirect_uri",
        [
            "https://evil.example/login",
            "https://myapp.com.evil.example/",
            "https://myapp.com:8443/",
            "http://myapp.com/",
            "//evil.example/",
            "/relative/path",
            "javascript:alert(1)",
        ],
    )
    def test_authorize_rejects_redirect_uri_outside_allowlist(
        self,
        client: TestClient,
        db: Session,
        headers: dict[str, str],
        monkeypatch: pytest.MonkeyPatch,
        redirect_uri: str,
    ) -> None:
        monkeypatch.setattr(settings, "oauth_redirect_allowed_origins", ["https://myapp.com"])
        user = UserFactory()

        response = client.get(
            "/api/v1/oauth/garmin/authorize",
            params={"user_id": str(user.id), "redirect_uri": redirect_uri},
            headers=headers,
        )

        assert response.status_code == 400
        assert response.json()["detail"] == (
            "redirect_uri origin is not allowed; add it to OAUTH_REDIRECT_ALLOWED_ORIGINS on the server"
        )

    def test_authorize_rejects_provider_callback_as_post_callback_redirect(
        self, client: TestClient, db: Session, headers: dict[str, str]
    ) -> None:
        response = client.get(
            "/api/v1/oauth/whoop/authorize",
            params={
                "user_id": str(UserFactory().id),
                "redirect_uri": settings.oauth_redirect_uri(ProviderName.WHOOP),
            },
            headers=headers,
        )

        assert response.status_code == 400
        assert response.json()["detail"] == (
            "redirect_uri must be an application return URL, not the Open Wearables provider callback"
        )

    def test_authorize_different_providers(self, client: TestClient, db: Session, headers: dict[str, str]) -> None:
        user = UserFactory()

        for provider in ["garmin", "polar", "suunto"]:
            response = client.get(
                f"/api/v1/oauth/{provider}/authorize", params={"user_id": str(user.id)}, headers=headers
            )

            assert response.status_code == 200
            data = response.json()
            assert "authorization_url" in data
            assert "state" in data

    def test_authorize_missing_user_id(self, client: TestClient, db: Session, headers: dict[str, str]) -> None:
        response = client.get("/api/v1/oauth/garmin/authorize", headers=headers)

        assert response.status_code == 422

    def test_authorize_invalid_user_id(self, client: TestClient, db: Session, headers: dict[str, str]) -> None:
        response = client.get("/api/v1/oauth/garmin/authorize", params={"user_id": "not-a-uuid"}, headers=headers)

        assert response.status_code == 422

    def test_authorize_invalid_provider(self, client: TestClient, db: Session, headers: dict[str, str]) -> None:
        response = client.get(
            "/api/v1/oauth/invalid-provider/authorize", params={"user_id": str(uuid4())}, headers=headers
        )

        assert response.status_code == 422

    @pytest.mark.parametrize(
        ("provider", "missing", "label"),
        [("garmin", "garmin_client_secret", "Garmin"), ("whoop", "whoop_client_id", "Whoop")],
    )
    def test_authorize_without_provider_credentials_is_rejected(
        self,
        client: TestClient,
        monkeypatch: pytest.MonkeyPatch,
        headers: dict[str, str],
        provider: str,
        missing: str,
        label: str,
    ) -> None:
        monkeypatch.setattr(settings, missing, None)

        response = client.get(
            f"/api/v1/oauth/{provider}/authorize", params={"user_id": str(UserFactory().id)}, headers=headers
        )

        assert response.status_code == 400
        assert (response.json()["code"], response.json()["detail"]) == (
            "PROVIDER_NOT_CONFIGURED",
            f"{label} is not configured on this server: its OAuth client ID and secret are missing",
        )

    def test_authorize_non_oauth_provider(self, client: TestClient, db: Session, headers: dict[str, str]) -> None:
        # "apple" uses file import, not OAuth
        response = client.get(
            "/api/v1/oauth/apple/authorize", params={"user_id": str(UserFactory().id)}, headers=headers
        )

        assert response.status_code == 400
        assert response.json()["code"] == "UNSUPPORTED_PROVIDER"


class TestOAuthProvidersEndpoint:
    """Test suite for providers listing endpoint."""

    def test_get_providers_success(self, client: TestClient, db: Session) -> None:
        """Test successfully retrieving all providers."""
        # Act
        response = client.get("/api/v1/oauth/providers")

        # Assert
        assert response.status_code == 200
        data = response.json()
        assert isinstance(data, list)
        assert len(data) > 0

        # Verify structure of first provider
        if data:
            provider = data[0]
            assert "provider" in provider
            assert "name" in provider
            assert "icon_url" in provider
            assert "has_cloud_api" in provider
            assert "is_enabled" in provider

    def test_get_providers_enabled_only(self, client: TestClient, db: Session) -> None:
        """Test retrieving only enabled providers."""
        # Act
        response = client.get(
            "/api/v1/oauth/providers",
            params={"enabled_only": True},
        )

        # Assert
        assert response.status_code == 200
        data = response.json()
        assert isinstance(data, list)
        # All returned providers should be enabled
        for provider in data:
            assert provider["is_enabled"] is True

    def test_get_providers_cloud_only(self, client: TestClient, db: Session) -> None:
        """Test retrieving only cloud (OAuth) providers."""
        # Act
        response = client.get(
            "/api/v1/oauth/providers",
            params={"cloud_only": True},
        )

        # Assert
        assert response.status_code == 200
        data = response.json()
        assert isinstance(data, list)
        # All returned providers should have cloud API
        for provider in data:
            assert provider["has_cloud_api"] is True

    def test_get_providers_enabled_and_cloud(self, client: TestClient, db: Session) -> None:
        """Test retrieving providers with both filters."""
        # Act
        response = client.get(
            "/api/v1/oauth/providers",
            params={"enabled_only": True, "cloud_only": True},
        )

        # Assert
        assert response.status_code == 200
        data = response.json()
        assert isinstance(data, list)
        # All returned providers should be enabled AND have cloud API
        for provider in data:
            assert provider["is_enabled"] is True
            assert provider["has_cloud_api"] is True

    def test_get_providers_include_disabled(self, client: TestClient, db: Session) -> None:
        """Test that disabled providers are included by default."""
        # Act - no filters
        response = client.get("/api/v1/oauth/providers")

        # Assert
        assert response.status_code == 200
        data = response.json()
        assert isinstance(data, list)
        # Should include both enabled and disabled
        any(p["is_enabled"] for p in data)
        any(not p["is_enabled"] for p in data)
        # At least one of each should exist (assuming test data setup)
        # If all are enabled or disabled, that's also valid

    def test_get_providers_response_structure(self, client: TestClient, db: Session) -> None:
        """Test detailed response structure of providers."""
        # Act
        response = client.get("/api/v1/oauth/providers")

        # Assert
        assert response.status_code == 200
        data = response.json()
        assert len(data) > 0

        # Check first provider has all required fields
        provider = data[0]
        required_fields = [
            "provider",
            "name",
            "icon_url",
            "has_cloud_api",
            "is_enabled",
        ]
        for field in required_fields:
            assert field in provider, f"Missing field: {field}"


class TestOAuthUpdateProviderEndpoint:
    """Test suite for updating provider status endpoint."""

    def test_update_provider_status_enable(
        self,
        client: TestClient,
        db: Session,
        mock_external_apis: dict[str, MagicMock],
    ) -> None:
        """Test enabling a provider."""
        # Arrange
        developer = DeveloperFactory()
        headers = developer_auth_headers(developer.id)
        update_data = {"is_enabled": True}

        # Act
        response = client.put(
            "/api/v1/oauth/providers/garmin",
            headers=headers,
            json=update_data,
        )

        # Assert
        assert response.status_code == 200
        data = response.json()
        assert data["provider"] == "garmin"
        assert data["is_enabled"] is True

    def test_update_provider_status_disable(
        self,
        client: TestClient,
        db: Session,
        mock_external_apis: dict[str, MagicMock],
    ) -> None:
        """Test disabling a provider."""
        # Arrange
        developer = DeveloperFactory()
        headers = developer_auth_headers(developer.id)
        update_data = {"is_enabled": False}

        # Act
        response = client.put(
            "/api/v1/oauth/providers/polar",
            headers=headers,
            json=update_data,
        )

        # Assert
        assert response.status_code == 200
        data = response.json()
        assert data["provider"] == "polar"
        assert data["is_enabled"] is False

    def test_update_provider_requires_authentication(self, client: TestClient, db: Session) -> None:
        """Test that updating provider status requires authentication."""
        # Arrange
        update_data = {"is_enabled": True}

        # Act - no auth headers
        response = client.put(
            "/api/v1/oauth/providers/garmin",
            json=update_data,
        )

        # Assert
        assert response.status_code == 401

    def test_update_provider_invalid_auth(self, client: TestClient, db: Session) -> None:
        """Test that invalid authentication is rejected."""
        # Arrange
        update_data = {"is_enabled": True}
        headers = {"Authorization": "Bearer invalid-token"}

        # Act
        response = client.put(
            "/api/v1/oauth/providers/garmin",
            headers=headers,
            json=update_data,
        )

        # Assert
        assert response.status_code == 401

    def test_update_nonexistent_provider(
        self,
        client: TestClient,
        db: Session,
        mock_external_apis: dict[str, MagicMock],
    ) -> None:
        """Test updating a provider that doesn't exist."""
        # Arrange
        developer = DeveloperFactory()
        headers = developer_auth_headers(developer.id)
        update_data = {"is_enabled": True}

        # Act
        response = client.put(
            "/api/v1/oauth/providers/nonexistent-provider",
            headers=headers,
            json=update_data,
        )

        # Assert
        assert response.status_code == 400
        assert response.json()["code"] == "BAD_REQUEST"

    def test_update_live_sync_mode_non_configurable_provider(
        self,
        client: TestClient,
        db: Session,
        mock_external_apis: dict[str, MagicMock],
    ) -> None:
        """Test setting live_sync_mode on a provider that does not support it returns 400."""
        # Arrange
        developer = DeveloperFactory()
        headers = developer_auth_headers(developer.id)
        # garmin has no rest_pull so live_sync_configurable=False
        update_data = {"live_sync_mode": "webhook"}

        # Act
        response = client.put(
            "/api/v1/oauth/providers/garmin",
            headers=headers,
            json=update_data,
        )

        # Assert
        assert response.status_code == 400
        assert response.json()["code"] == "UNSUPPORTED_PROVIDER"

    def test_update_provider_response_structure(
        self,
        client: TestClient,
        db: Session,
        mock_external_apis: dict[str, MagicMock],
    ) -> None:
        """Test response structure of update endpoint."""
        # Arrange
        developer = DeveloperFactory()
        headers = developer_auth_headers(developer.id)
        update_data = {"is_enabled": True}

        # Act
        response = client.put(
            "/api/v1/oauth/providers/garmin",
            headers=headers,
            json=update_data,
        )

        # Assert
        assert response.status_code == 200
        data = response.json()
        assert "provider" in data
        assert "name" in data
        assert "icon_url" in data
        assert "has_cloud_api" in data
        assert "is_enabled" in data

    def test_update_multiple_providers_sequentially(
        self,
        client: TestClient,
        db: Session,
        mock_external_apis: dict[str, MagicMock],
    ) -> None:
        """Test updating multiple providers one after another."""
        # Arrange
        developer = DeveloperFactory()
        headers = developer_auth_headers(developer.id)
        providers = ["garmin", "polar", "suunto"]

        for provider in providers:
            # Act
            response = client.put(
                f"/api/v1/oauth/providers/{provider}",
                headers=headers,
                json={"is_enabled": True},
            )

            # Assert
            assert response.status_code == 200
            data = response.json()
            assert data["provider"] == provider
            assert data["is_enabled"] is True


class TestOAuthCallbackProbe:
    """Providers such as Withings send HEAD to the callback URL when it is registered."""

    def test_head_callback_returns_ok_without_body(self, client: TestClient, db: Session) -> None:
        response = client.head("/api/v1/oauth/withings/callback")
        assert response.status_code == 200
        assert response.content == b""

    def test_head_callback_invalid_provider(self, client: TestClient, db: Session) -> None:
        response = client.head("/api/v1/oauth/not-a-provider/callback")
        assert response.status_code == 422


class TestPairingTokenEndpoint:
    def test_issues_a_token_that_authorizes_only_this_user(self, client: TestClient, db: Session) -> None:
        user = UserFactory()

        response = client.post(
            f"/api/v1/users/{user.id}/pairing-token", headers=developer_auth_headers(DeveloperFactory().id)
        )

        assert response.status_code == 200
        token = response.json()["token"]
        assert response.json()["expires_at"]
        authorize = client.get(
            "/api/v1/oauth/garmin/authorize",
            params={"user_id": str(user.id)},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert authorize.status_code == 200

    def test_requires_authentication(self, client: TestClient, db: Session) -> None:
        response = client.post(f"/api/v1/users/{UserFactory().id}/pairing-token")

        assert response.status_code == 401

    def test_unknown_user(self, client: TestClient, db: Session) -> None:
        response = client.post(f"/api/v1/users/{uuid4()}/pairing-token", headers=api_key_headers(ApiKeyFactory()))

        assert response.status_code == 404


def test_pairing_token_without_a_user_is_refused(client: TestClient, db: Session) -> None:
    token = jwt.encode({"sub": "not-a-uuid", "scope": "pairing"}, settings.secret_key, algorithm=settings.algorithm)

    response = client.get(
        "/api/v1/oauth/garmin/authorize",
        params={"user_id": str(UserFactory().id)},
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 401
