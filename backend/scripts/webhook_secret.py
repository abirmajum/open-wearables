"""Print the webhook secret to register with a provider that you configure by hand.

Run it where the backend runs, so that it sees the same SECRET_KEY and *_WEBHOOK_SECRET:

    cd backend && uv run python scripts/webhook_secret.py suunto
    docker compose exec app uv run python scripts/webhook_secret.py google_health

Unless set explicitly, these secrets are derived from SECRET_KEY (see app/config.py),
so a provider never receives SECRET_KEY itself. Treat the output like a password.
"""

import argparse
import sys

from pydantic import SecretStr

from app.config import settings

SECRETS: dict[str, tuple[str, SecretStr | None]] = {
    "suunto": ("notification secret in the Suunto developer portal", settings.suunto_webhook_secret),
    "google_health": ('subscriber endpointAuthorization.secret, after "Bearer "', settings.google_webhook_secret),
}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("provider", choices=sorted(SECRETS))
    provider = parser.parse_args().provider
    where, secret = SECRETS[provider]
    if secret is None:
        parser.error(f"no webhook secret configured for {provider}")
    print(f"Set this value as the {where}:", file=sys.stderr)
    print(secret.get_secret_value())


if __name__ == "__main__":
    main()
