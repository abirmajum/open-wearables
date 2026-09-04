from uuid import UUID

from sqlalchemy import String
from sqlalchemy.orm import Mapped, mapped_column

from app.database import BaseDbModel
from app.mappings import FKDeveloper, PrimaryKey, str_64


class ApiKey(BaseDbModel):
    """Global API key for external service access."""

    __tablename__ = "api_key"

    id: Mapped[PrimaryKey[UUID]]
    key_hash: Mapped[str_64] = mapped_column(unique=True, index=True)
    display_prefix: Mapped[str] = mapped_column(String(12))
    name: Mapped[str]
    created_by: Mapped[FKDeveloper | None]

    def __repr__(self) -> str:
        return (
            f"<ApiKey(id={self.id!r}, name={self.name!r}, "
            f"display_prefix={self.display_prefix!r}, created_by={self.created_by!r})>"
        )
