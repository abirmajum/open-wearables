import secrets
from hashlib import sha256
from logging import Logger, getLogger
from typing import Annotated
from uuid import UUID

from fastapi import Depends, Header, HTTPException

from app.database import DbSession
from app.models import ApiKey, Developer
from app.repositories.api_key_repository import ApiKeyRepository
from app.schemas.model_crud.credentials import ApiKeyCreate, ApiKeyRead, ApiKeyUpdate, ApiKeyWithSecret
from app.services.services import AppService
from app.utils.auth import get_current_developer_optional


class ApiKeyService(AppService[ApiKeyRepository, ApiKey, ApiKeyCreate, ApiKeyUpdate]):
    def __init__(self, log: Logger, **kwargs):
        super().__init__(
            crud_model=ApiKeyRepository,
            model=ApiKey,
            log=log,
            **kwargs,
        )

    @staticmethod
    def _generate_secret() -> str:
        return f"sk-{secrets.token_hex(32)}"

    @staticmethod
    def hash_secret(secret: str) -> str:
        return sha256(secret.encode("utf-8")).hexdigest()

    @staticmethod
    def _display_prefix(secret: str) -> str:
        return secret[:11]

    @staticmethod
    def _credential_response(api_key: ApiKey, secret: str) -> ApiKeyWithSecret:
        public_fields = ApiKeyRead.model_validate(api_key).model_dump()
        return ApiKeyWithSecret(**public_fields, secret=secret)

    def create_api_key(
        self,
        db: DbSession,
        created_by: UUID | None,
        name: str = "Default",
    ) -> ApiKeyWithSecret:
        secret = self._generate_secret()
        creator = ApiKeyCreate(
            id=UUID(bytes=secrets.token_bytes(16), version=4),
            key_hash=self.hash_secret(secret),
            display_prefix=self._display_prefix(secret),
            name=name,
            created_by=created_by,
        )
        api_key = self.create(db, creator)
        self.logger.debug(f"Created API key {api_key.id} by developer {created_by} with name {name}")
        return self._credential_response(api_key, secret)

    def list_api_keys(self, db: DbSession) -> list[ApiKey]:
        """List all API keys ordered by creation date."""
        keys = self.crud.get_all_ordered(db)
        self.logger.debug(f"Listed {len(keys)} API keys")
        return keys

    def rotate_api_key(self, db: DbSession, key_id: UUID) -> ApiKeyWithSecret:
        api_key = self.get(db, key_id, raise_404=True)
        assert api_key is not None
        secret = self._generate_secret()
        rotated = self.crud.rotate_secret(
            db,
            api_key,
            key_hash=self.hash_secret(secret),
            display_prefix=self._display_prefix(secret),
        )
        self.logger.debug(f"Rotated API key {rotated.id}")
        return self._credential_response(rotated, secret)

    def validate_api_key(self, db: DbSession, key: str) -> ApiKey:
        """Validate API key exists in database. Raises 401 if invalid."""
        if not (api_key := self.crud.get_by_hash(db, self.hash_secret(key))):
            raise HTTPException(status_code=401, detail="Invalid or missing API key")
        return api_key


api_key_service = ApiKeyService(log=getLogger(__name__))


async def _require_api_key(
    db: DbSession,
    developer: Developer | None = Depends(get_current_developer_optional),
    x_open_wearables_api_key: str | None = Header(None, alias="X-Open-Wearables-API-Key"),
) -> str:
    if developer:
        return str(developer.id)
    if x_open_wearables_api_key:
        return str(api_key_service.validate_api_key(db, x_open_wearables_api_key).id)
    raise HTTPException(status_code=401, detail="Authentication required: provide JWT token or API key")


ApiKeyDep = Annotated[str, Depends(_require_api_key)]
