"""Ultrahuman Ring Air OAuth 2.0 implementation."""

from typing import Any

import httpx

from app.config import settings
from app.repositories.user_connection_repository import UserConnectionRepository
from app.repositories.user_repository import UserRepository
from app.schemas.auth import AuthenticationMethod
from app.schemas.enums import ProviderName
from app.schemas.model_crud.credentials import (
    OAuthTokenResponse,
    ProviderCredentials,
    ProviderEndpoints,
)
from app.services.providers.templates.base_oauth import BaseOAuthTemplate
from app.utils.structured_logging import get_logger

logger = get_logger(__name__)


def provider_user_info(profile_data: Any, user_id: str) -> dict[str, str | None]:
    """The Ultrahuman account id and username in a /user_data/user_info answer.

    Ultrahuman documents no example answer: the fields are read at the top level, else inside the ``data`` envelope
    its other endpoints use. A profile without an id is logged with its field names, never their values.
    """
    profile = profile_data if isinstance(profile_data, dict) else {}
    if "user_id" not in profile and isinstance(profile.get("data"), dict):
        profile = profile["data"]
    provider_user_id = profile.get("user_id")
    provider_username = profile.get("username") or profile.get("email")
    if not provider_user_id:
        logger.warning(
            "Ultrahuman user profile has no user_id; the per-account pull lock stays off",
            provider="ultrahuman",
            user_id=user_id,
            profile_fields=sorted(str(key) for key in profile),
        )
    return {
        "user_id": str(provider_user_id) if provider_user_id else None,
        "username": provider_username if isinstance(provider_username, str) else None,
    }


class UltrahumanOAuth(BaseOAuthTemplate):
    """Ultrahuman Partnership API OAuth 2.0 implementation.

    Ultrahuman uses standard OAuth 2.0 with authorization code grant.
    API documentation: https://vision.ultrahuman.com/developer-docs
    """

    def __init__(
        self,
        user_repo: UserRepository,
        connection_repo: UserConnectionRepository,
        provider_name: str,
        api_base_url: str,
    ):
        super().__init__(user_repo, connection_repo, provider_name, api_base_url)

    @property
    def endpoints(self) -> ProviderEndpoints:
        """OAuth endpoints for authorization and token exchange."""
        return ProviderEndpoints(
            authorize_url="https://auth.ultrahuman.com/authorise",
            token_url="https://partner.ultrahuman.com/api/partners/oauth/token",
        )

    @property
    def credentials(self) -> ProviderCredentials:
        """OAuth credentials from environment variables."""
        return ProviderCredentials(
            client_id=settings.ultrahuman_client_id or "",
            client_secret=(
                settings.ultrahuman_client_secret.get_secret_value() if settings.ultrahuman_client_secret else ""
            ),
            redirect_uri=settings.oauth_redirect_uri(ProviderName.ULTRAHUMAN),
            default_scope=settings.ultrahuman_default_scope,
        )

    # OAuth configuration
    use_pkce = False  # Ultrahuman doesn't require PKCE
    auth_method = AuthenticationMethod.BODY  # Credentials in request body

    def _get_provider_user_info(self, token_response: OAuthTokenResponse, user_id: str) -> dict[str, str | None]:
        """Fetches Ultrahuman user profile via API.

        Returns user ID and username from Ultrahuman profile data.
        Endpoint: /user_data/user_info

        Never fails the connect: without an id the connection works, but the per-account pull lock is off until a
        later sync fills it in (``Ultrahuman247Data._fill_provider_user_id``).
        """
        try:
            profile_response = httpx.get(
                f"{self.api_base_url}/user_data/user_info",
                headers={"Authorization": f"Bearer {token_response.access_token}"},
                timeout=30.0,
            )
            profile_response.raise_for_status()
            return provider_user_info(profile_response.json(), user_id)
        except httpx.HTTPError as e:
            logger.warning(
                "Ultrahuman user profile not fetched at connect; retried on a later sync",
                exc_info=e,
                provider="ultrahuman",
                user_id=user_id,
            )
            return {"user_id": None, "username": None}
        except Exception as e:
            logger.error(
                "Unexpected error fetching Ultrahuman user profile",
                exc_info=e,
                provider="ultrahuman",
                user_id=user_id,
            )
            return {"user_id": None, "username": None}

    def deregister_user(self, access_token: str, provider_user_id: str | None = None) -> None:
        """Revoke Ultrahuman OAuth access for this user."""

        response = httpx.post(
            "https://partner.ultrahuman.com/api/partners/oauth/revoke",
            data={
                "token": access_token,
                "client_id": self.credentials.client_id,
                "client_secret": self.credentials.client_secret,
            },
            timeout=30.0,
        )
        response.raise_for_status()
