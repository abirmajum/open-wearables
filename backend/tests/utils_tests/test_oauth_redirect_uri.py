"""Tests for provider OAuth callback URI generation."""

from unittest.mock import patch

import pytest

from app.api.routes.v1.oauth import _is_provider_callback_url
from app.config import Settings, settings
from app.schemas.enums import ProviderName


def test_whoop_callback_uri_is_derived_from_production_api_base_url() -> None:
    settings = Settings(secret_key="test-secret", api_base_url="https://ow.rumihq.com")

    assert settings.oauth_redirect_uri(ProviderName.WHOOP) == "https://ow.rumihq.com/api/v1/oauth/whoop/callback"


def test_oauth_callback_uri_does_not_duplicate_a_trailing_base_url_slash() -> None:
    settings = Settings(secret_key="test-secret", api_base_url="https://ow.rumihq.com/")

    assert settings.oauth_redirect_uri(ProviderName.WHOOP) == "https://ow.rumihq.com/api/v1/oauth/whoop/callback"


@pytest.mark.parametrize(
    "redirect_uri",
    [
        "https://ow.rumihq.com:443/api/v1/oauth/whoop/callback",
        "https://ow.rumihq.com/api/v1/oauth/whoop%2Fcallback",
    ],
)
def test_callback_loop_guard_normalises_equivalent_callback_urls(redirect_uri: str) -> None:
    with patch.object(settings, "api_base_url", "https://ow.rumihq.com"):
        assert _is_provider_callback_url(ProviderName.WHOOP, redirect_uri) is True
