from datetime import datetime
from typing import Any

from sqlalchemy import Index
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.database import BaseDbModel
from app.mappings import FKUser, PrimaryKey, str_64


class ProviderCycle(BaseDbModel):
    """Latest revision of a provider-defined physiological interval, never a daily total."""

    __tablename__ = "provider_cycle"
    __table_args__ = (Index("ix_provider_cycle_user_updated", "user_id", "updated_at", "id"),)

    id: Mapped[PrimaryKey[str_64]]
    user_id: Mapped[PrimaryKey[FKUser]]
    provider: Mapped[PrimaryKey[str_64]]
    updated_at: Mapped[datetime]
    fetched_at: Mapped[datetime]
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB)
