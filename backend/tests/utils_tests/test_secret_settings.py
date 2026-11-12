"""SECRET_KEY is refused at startup when guessable; secrets shared with providers are derived from it."""

import pytest
from pydantic import ValidationError

from app.config import MIN_SECRET_KEY_LENGTH, Settings
from app.utils.config_utils import derive_secret

STRONG_SECRET_KEY = "a" * MIN_SECRET_KEY_LENGTH


class TestSecretKeyValidation:
    @pytest.mark.parametrize(
        "value", ["secret-key-str", "a" * (MIN_SECRET_KEY_LENGTH - 1)], ids=["old_default", "short"]
    )
    def test_short_key_is_rejected(self, value: str) -> None:
        with pytest.raises(ValidationError, match=f"at least {MIN_SECRET_KEY_LENGTH} characters"):
            Settings(secret_key=value)

    def test_empty_key_is_rejected(self) -> None:
        with pytest.raises(ValidationError, match="SECRET_KEY is not set. Run `make init`"):
            Settings(secret_key="")

    def test_missing_key_points_to_make_init(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("SECRET_KEY", raising=False)

        with pytest.raises(ValidationError, match="SECRET_KEY is not set. Run `make init`"):
            Settings()

    def test_key_of_minimum_length_is_accepted(self) -> None:
        settings = Settings(secret_key=STRONG_SECRET_KEY)

        assert settings.secret_key == STRONG_SECRET_KEY

    def test_rejected_key_is_not_echoed_in_the_error(self) -> None:
        real_but_short_key = "MyRealButShortSecret-123"

        with pytest.raises(ValidationError) as exc_info:
            Settings(secret_key=real_but_short_key)

        assert real_but_short_key not in str(exc_info.value)


class TestStravaWebhookVerifyToken:
    def _token(self, value: str | None) -> str:
        token = Settings(secret_key=STRONG_SECRET_KEY, strava_webhook_verify_token=value).strava_webhook_verify_token
        assert token is not None
        return token.get_secret_value()

    @pytest.mark.parametrize("value", [None, ""])
    def test_unset_token_is_derived_from_secret_key(self, value: str | None) -> None:
        token = self._token(value)

        assert token == derive_secret(STRONG_SECRET_KEY, "strava-webhook-verify-token")
        assert token != STRONG_SECRET_KEY

    def test_own_token_is_kept(self) -> None:
        assert self._token("b" * 64) == "b" * 64


class TestSharedWebhookSecrets:
    """Secrets a provider or the Svix server receives are derived from SECRET_KEY, never SECRET_KEY itself."""

    @pytest.mark.parametrize(
        ("field", "purpose"),
        [
            ("suunto_webhook_secret", "suunto-webhook-secret"),
            ("google_webhook_secret", "google-webhook-secret"),
            ("oura_webhook_verification_token", "oura-webhook-verification-token"),
            ("svix_jwt_secret", "svix-jwt-secret"),
        ],
    )
    @pytest.mark.parametrize("value", [None, ""])
    def test_unset_secret_is_derived_from_secret_key(self, field: str, purpose: str, value: str | None) -> None:
        secret = getattr(Settings(secret_key=STRONG_SECRET_KEY, **{field: value}), field)

        assert secret is not None
        assert secret.get_secret_value() == derive_secret(STRONG_SECRET_KEY, purpose)
        assert secret.get_secret_value() != STRONG_SECRET_KEY

    @pytest.mark.parametrize(
        "field",
        ["suunto_webhook_secret", "google_webhook_secret", "oura_webhook_verification_token", "svix_jwt_secret"],
    )
    def test_own_secret_is_kept(self, field: str) -> None:
        secret = getattr(Settings(secret_key=STRONG_SECRET_KEY, **{field: "b" * 64}), field)

        assert secret is not None
        assert secret.get_secret_value() == "b" * 64
