from datetime import datetime, time, timezone
from uuid import UUID

from sqlalchemy import delete, literal_column, or_, select, text, tuple_
from sqlalchemy.dialects.postgresql import insert

from app.database import DbSession
from app.models import DataPointSeries, DataPointSeriesArchive, DataSource, HealthScore
from app.models.provider_cycle import ProviderCycle
from app.repositories.data_point_series_repository import WriteCounts
from app.schemas.enums import HealthScoreCategory, ProviderName, SeriesType, get_series_type_id


class ProviderCycleRepository:
    def lock_user(self, db: DbSession, user_id: UUID) -> None:
        # One transaction owns snapshot revisions and every affected daily projection.
        db.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
            {"key": f"whoop-cycle-projection:{user_id}"},
        )

    def list_whoop(self, db: DbSession, user_id: UUID) -> list[ProviderCycle]:
        return list(
            db.scalars(
                select(ProviderCycle)
                .where(ProviderCycle.user_id == user_id, ProviderCycle.provider == "whoop")
                .execution_options(populate_existing=True)
            )
        )

    def clear_owned_projection(
        self, db: DbSession, user_id: UUID, days: set[datetime], owned_ids: set[UUID]
    ) -> tuple[set[tuple[datetime, int]], set[datetime]]:
        """Remove only rows this projector owns; unknown legacy/source rows remain untouched."""
        sources = select(DataSource.id).where(
            DataSource.user_id == user_id,
            DataSource.provider == ProviderName.WHOOP,
            DataSource.source == "whoop",
            DataSource.device_model.is_(None),
            DataSource.device_id.is_(None),
            DataSource.source_app_id.is_(None),
        )
        db.execute(
            delete(DataPointSeries).where(
                DataPointSeries.data_source_id.in_(sources),
                DataPointSeries.id.in_(owned_ids),
                DataPointSeries.recorded_at.in_(days),
                DataPointSeries.is_daily_total.is_(True),
            )
        )
        db.execute(
            delete(HealthScore).where(
                HealthScore.user_id == user_id,
                HealthScore.provider == ProviderName.WHOOP,
                HealthScore.category == HealthScoreCategory.STRAIN,
                HealthScore.event_record_id.is_(None),
                HealthScore.id.in_(owned_ids),
                HealthScore.recorded_at.in_(days),
            )
        )
        occupied_samples = set(
            db.execute(
                select(DataPointSeries.recorded_at, DataPointSeries.series_type_definition_id).where(
                    DataPointSeries.data_source_id.in_(sources), DataPointSeries.recorded_at.in_(days)
                )
            ).tuples()
        )
        # Archive rows no longer retain individual sample ownership: do not claim them.
        utc_days = {datetime.combine(day.astimezone(timezone.utc).date(), time.min, timezone.utc) for day in days}
        archived = list(
            db.execute(
                select(DataPointSeriesArchive.bucket_start_at, DataPointSeriesArchive.series_type_definition_id).where(
                    DataPointSeriesArchive.data_source_id.in_(sources),
                    DataPointSeriesArchive.bucket_start_at.in_(utc_days),
                    DataPointSeriesArchive.series_type_definition_id.in_(
                        [get_series_type_id(SeriesType.steps), get_series_type_id(SeriesType.total_energy)]
                    ),
                )
            ).tuples()
        )
        for day in days:
            occupied_samples.update(
                (day, kind)
                for stamp, kind in archived
                if stamp.astimezone(timezone.utc).date() == day.astimezone(timezone.utc).date()
            )
        occupied_scores = set(
            db.scalars(
                select(HealthScore.recorded_at).where(
                    HealthScore.user_id == user_id,
                    HealthScore.provider == ProviderName.WHOOP,
                    HealthScore.category == HealthScoreCategory.STRAIN,
                    HealthScore.recorded_at.in_(days),
                )
            )
        )
        return occupied_samples, occupied_scores

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
