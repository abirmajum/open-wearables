from datetime import datetime
from uuid import UUID

from sqlalchemy import literal_column, or_, select, tuple_
from sqlalchemy.dialects.postgresql import insert

from app.database import DbSession
from app.models.provider_cycle import ProviderCycle
from app.repositories.data_point_series_repository import WriteCounts


class ProviderCycleRepository:
    def upsert(self, db: DbSession, cycle: ProviderCycle) -> WriteCounts:
        stmt = insert(ProviderCycle).values(
            id=cycle.id,
            user_id=cycle.user_id,
            provider=cycle.provider,
            updated_at=cycle.updated_at,
            fetched_at=cycle.fetched_at,
            payload=cycle.payload,
        )
        result = db.execute(
            stmt.on_conflict_do_update(
                index_elements=[ProviderCycle.id, ProviderCycle.user_id, ProviderCycle.provider],
                set_={
                    "updated_at": stmt.excluded.updated_at,
                    "fetched_at": stmt.excluded.fetched_at,
                    "payload": stmt.excluded.payload,
                },
                where=or_(
                    stmt.excluded.updated_at > ProviderCycle.updated_at,
                    (stmt.excluded.updated_at == ProviderCycle.updated_at)
                    & (stmt.excluded.fetched_at > ProviderCycle.fetched_at),
                ),
            ).returning(literal_column("xmax = 0"))
        )
        inserted = result.scalar_one_or_none()
        return WriteCounts(int(inserted is True), int(inserted is False))

    def page(
        self,
        db: DbSession,
        user_id: UUID,
        start: datetime,
        end: datetime,
        limit: int,
        after: tuple[datetime, str] | None,
    ) -> list[ProviderCycle]:
        query = select(ProviderCycle).where(
            ProviderCycle.user_id == user_id,
            ProviderCycle.provider == "whoop",
            ProviderCycle.updated_at >= start,
            ProviderCycle.updated_at <= end,
        )
        if after is not None:
            query = query.where(tuple_(ProviderCycle.updated_at, ProviderCycle.id) > after)
        return list(db.scalars(query.order_by(ProviderCycle.updated_at, ProviderCycle.id).limit(limit + 1)))
