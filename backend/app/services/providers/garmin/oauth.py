import httpx

from app.config import settings
from app.schemas.auth import (
    AuthenticationMethod,
)
from app.schemas.enums import ProviderName
from app.schemas.model_crud.credentials import (
    OAuthTokenResponse,
    ProviderCredentials,
    ProviderEndpoints,
)
from app.services.providers.templates.base_oauth import BaseOAuthTemplate
from app.utils.exceptions import ProviderError
from app.utils.structured_logging import get_logger

logger = get_logger(__name__)


class GarminOAuth(BaseOAuthTemplate):
    """Garmin OAuth 2.0 with PKCE implementation."""

    @property
    def endpoints(self) -> ProviderEndpoints:
        return ProviderEndpoints(
            authorize_url="https://connect.garmin.com/oauth2Confirm",
            token_url="https://diauth.garmin.com/di-oauth2-service/oauth/token",
        )

    @property
    def credentials(self) -> ProviderCredentials:
        return ProviderCredentials(
            client_id=settings.garmin_client_id or "",
            client_secret=(settings.garmin_client_secret.get_secret_value() if settings.garmin_client_secret else ""),
            redirect_uri=settings.oauth_redirect_uri(ProviderName.GARMIN),
            default_scope=settings.garmin_default_scope,
        )

    use_pkce = True
    auth_method = AuthenticationMethod.BODY

    def deregister_user(self, access_token: str, provider_user_id: str | None = None) -> None:
        """Call Garmin's user deregistration endpoint to remove the app association."""
        response = httpx.delete(
            f"{self.api_base_url}/partner-gateway/rest/user/registration",
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=30.0,
        )
        response.raise_for_status()

    def get_user_id(self, access_token: str) -> str | None:
        response = httpx.get(
            f"{self.api_base_url}/partner-gateway/rest/user/id",
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=30.0,
        )
        response.raise_for_status()
        return response.json().get("userId")

    def _get_provider_user_info(self, token_response: OAuthTokenResponse, user_id: str) -> dict[str, str | None]:
        """Fetches Garmin user ID and API permissions.

        Raises ProviderError without a user ID: Garmin's webhooks name only that ID, so a
        connection saved without it would look active and never receive data.
        """
        upstream_status = None
        try:
            user_id_response = httpx.get(
                f"{self.api_base_url}/partner-gateway/rest/user/id",
                headers={"Authorization": f"Bearer {token_response.access_token}"},
                timeout=30.0,
            )
            upstream_status = user_id_response.status_code
            user_id_response.raise_for_status()
            provider_user_id = user_id_response.json().get("userId")
        except Exception as e:
            logger.warning("Garmin user ID lookup failed", provider="garmin", user_id=user_id, exc_info=e)
            provider_user_id = None
        if not provider_user_id:
            raise ProviderError(
                "garmin",
                "Garmin did not return the account's user ID, so the connection was not saved. Try connecting again.",
                upstream_status=upstream_status,
            )

        # Fetch permissions (best-effort - fail returns scope as None)
        scope: str | None = None
        try:
            permissions_response = httpx.get(
                f"{self.api_base_url}/partner-gateway/rest/user/permissions",
                headers={"Authorization": f"Bearer {token_response.access_token}"},
                timeout=30.0,
            )
            permissions_response.raise_for_status()
            data = permissions_response.json()
            # Response format: {"permissions": ["ACTIVITY_EXPORT", "HEALTH_EXPORT", ...]}
            permissions = data.get("permissions", [])
            if permissions:
                scope = " ".join(sorted(permissions))
        except Exception as e:
            logger.warning(
                "Failed to fetch Garmin permissions, will retry via webhook",
                provider="garmin",
                provider_user_id=provider_user_id,
                exc_info=e,
            )

        return {"user_id": provider_user_id, "username": None, "scope": scope}
