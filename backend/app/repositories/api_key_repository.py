from app.database import DbSession
from app.models import ApiKey
from app.repositories.repositories import CrudRepository
from app.schemas.model_crud.credentials import ApiKeyCreate, ApiKeyUpdate


class ApiKeyRepository(CrudRepository[ApiKey, ApiKeyCreate, ApiKeyUpdate]):
    def __init__(self, model: type[ApiKey]):
        super().__init__(model)

    def get_all_ordered(self, db_session: DbSession) -> list[ApiKey]:
        """Get all API keys ordered by creation date descending."""
        return db_session.query(self.model).order_by(self.model.created_at.desc()).all()

    def get_by_hash(self, db_session: DbSession, key_hash: str) -> ApiKey | None:
        return db_session.query(self.model).filter(self.model.key_hash == key_hash).one_or_none()

    def rotate_secret(self, db_session: DbSession, api_key: ApiKey, key_hash: str, display_prefix: str) -> ApiKey:
        api_key.key_hash = key_hash
        api_key.display_prefix = display_prefix
        db_session.add(api_key)
        db_session.commit()
        db_session.refresh(api_key)
        return api_key
