from datetime import date, datetime, timedelta, timezone
from typing import Any, cast
from uuid import UUID

from sqlalchemy import and_, asc, desc, text, tuple_
from sqlalchemy.dialects.postgresql import insert

from app.database import DbSession
from app.models import DataSource, HealthScore
from app.repositories.repositories import CrudRepository
from app.schemas.enums import HealthScoreCategory, ProviderName
from app.schemas.model_crud.activities import HealthScoreCreate, HealthScoreQueryParams, HealthScoreUpdate
from app.utils.pagination import decode_cursor


class HealthScoreRepository(CrudRepository[HealthScore, HealthScoreCreate, HealthScoreUpdate]):
    def upsert_whoop_recovery(self, db_session: DbSession, creator: HealthScoreCreate) -> HealthScore:
        """Replace a fetched WHOOP recovery without changing its public ID or date.

        WHOOP's created_at is the existing recovery identity; updated_at must not
        move a correction to another day. Keep other providers' insert semantics.
        """
        if (
            creator.provider != ProviderName.WHOOP
            or creator.category != HealthScoreCategory.RECOVERY
            or creator.event_record_id is not None
        ):
            raise ValueError("whoop_recovery_required")
        stmt = insert(HealthScore).values(creator.model_dump())
        stmt = stmt.on_conflict_do_update(
            constraint="uq_health_score_user_provider_category_time",
            set_={
                "value": stmt.excluded.value,
                "qualifier": stmt.excluded.qualifier,
                "components": stmt.excluded.components,
            },
        ).returning(HealthScore)
        return db_session.scalars(stmt, execution_options={"populate_existing": True}).one()

    def upsert_event_score(self, db_session: DbSession, creator: HealthScoreCreate) -> HealthScore:
        """Refresh a provider event score while preserving its public identity.

        The event link is authoritative; timestamp lookup attaches legacy scores
        that predate event links. Serialize this account/category's writes so a
        webhook and history replay cannot race the two unique constraints.
        """
        if creator.event_record_id is None:
            raise ValueError("event_score_requires_record")
        db_session.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
            {"key": f"event-score:{creator.user_id}:{creator.provider}:{creator.category}"},
        )
        scope = db_session.query(HealthScore).filter(
            HealthScore.user_id == creator.user_id,
            HealthScore.provider == creator.provider,
            HealthScore.category == creator.category,
        )
        existing = scope.filter(HealthScore.event_record_id == creator.event_record_id).first()
        if existing is None:
            existing = scope.filter(HealthScore.recorded_at == creator.recorded_at).first()
            if existing is not None and existing.event_record_id not in (None, creator.event_record_id):
                raise ValueError("event_score_identity_conflict")
        values = creator.model_dump()
        if existing is None:
            existing = HealthScore(**values)
            db_session.add(existing)
        else:
            for key, value in values.items():
                if key != "id":
                    setattr(existing, key, value)
        db_session.flush()
        return existing

    def get_by_all_components(self, db_session: DbSession, components: list[str]) -> list[HealthScore]:
        """Return health scores whose components JSONB contains all specified keys (?& operator)."""
        return db_session.query(HealthScore).filter(HealthScore.components.has_all(components)).all()

    def get_by_any_component(self, db_session: DbSession, components: list[str]) -> list[HealthScore]:
        """Return health scores whose components JSONB contains any of the specified keys (?| operator)."""
        return db_session.query(HealthScore).filter(HealthScore.components.has_any(components)).all()

    def get_with_filters(
        self,
        db_session: DbSession,
        user_id: UUID,
        params: HealthScoreQueryParams,
    ) -> tuple[list[HealthScore], int]:
        filters = [HealthScore.user_id == user_id]

        if params.category:
            filters.append(HealthScore.category == params.category)
        if params.provider:
            filters.append(HealthScore.provider == params.provider)
        if params.data_source_id:
            filters.append(HealthScore.data_source_id == params.data_source_id)
        if params.start_datetime:
            filters.append(HealthScore.recorded_at >= params.start_datetime)
        if params.end_datetime:
            filters.append(HealthScore.recorded_at < params.end_datetime)

        query = db_session.query(HealthScore).filter(and_(*filters))

        total_count = query.count()
        results = query.order_by(desc(HealthScore.recorded_at)).offset(params.offset).limit(params.limit).all()
        return results, total_count

    def bulk_create(self, db_session: DbSession, creators: list[HealthScoreCreate]) -> None:
        """Bulk insert health scores, doing nothing on conflict with the unique constraint."""
        if not creators:
            return

        values = [c.model_dump() for c in creators]

        stmt = insert(HealthScore).values(values).on_conflict_do_nothing()
        db_session.execute(stmt)
        # Caller is responsible for commit — allows batching with other operations

    def get_latest_by_category(
        self,
        db_session: DbSession,
        user_id: UUID,
        category: HealthScoreCategory,
    ) -> HealthScore | None:
        """Return the most recent health score for a given category and user."""
        return (
            db_session.query(HealthScore)
            .filter(HealthScore.user_id == user_id, HealthScore.category == category)
            .order_by(desc(HealthScore.recorded_at))
            .first()
        )

    def delete_for_user_date(
        self,
        db_session: DbSession,
        user_id: UUID,
        score_date: date,
        category: HealthScoreCategory,
        provider: str = "internal",
    ) -> int:
        """Delete health scores matching user/category/provider/date without loading objects.

        Caller is responsible for commit. Returns deleted row count.
        Matches the whole day rather than an exact timestamp: sleep scores are anchored to
        the session's local wake time, and older rows sit at midnight of the same date.
        """
        day_start = datetime(score_date.year, score_date.month, score_date.day, tzinfo=timezone.utc)
        return (
            db_session.query(HealthScore)
            .filter(
                HealthScore.user_id == user_id,
                HealthScore.provider == provider,
                HealthScore.category == category,
                HealthScore.recorded_at >= day_start,
                HealthScore.recorded_at < day_start + timedelta(days=1),
            )
            .delete(synchronize_session=False)
        )

    def get_recovery_summaries(
        self,
        db_session: DbSession,
        user_id: UUID,
        start_date: datetime,
        end_date: datetime,
        cursor: str | None,
        limit: int,
    ) -> list[dict[str, Any]]:
        """Get recovery health scores for a date range with cursor-based pagination.

        Returns list of dicts with keys: recovery_date, provider, source, device_model,
        device_type, record_id, recorded_at, recovery_score, resting_heart_rate,
        hrv_rmssd_milli, spo2_percentage.
        Fetches limit+1 rows so callers can detect has_more without a separate COUNT query.
        Ordering matches get_sleep_summaries: ASC by default, DESC when paginating backward.
        """
        # Outer join so scores without a data_source (older rows) still come back.
        query = (
            db_session.query(HealthScore, DataSource)
            .outerjoin(DataSource, HealthScore.data_source_id == DataSource.id)
            .filter(
                HealthScore.user_id == user_id,
                HealthScore.category == HealthScoreCategory.RECOVERY,
                HealthScore.recorded_at >= start_date,
                HealthScore.recorded_at < end_date,
            )
        )

        if cursor:
            cursor_ts, cursor_id, direction = decode_cursor(cursor)
            if direction == "prev":
                query = query.filter(tuple_(HealthScore.recorded_at, HealthScore.id) < (cursor_ts, cursor_id)).order_by(
                    desc(HealthScore.recorded_at), desc(HealthScore.id)
                )
            else:
                query = query.filter(tuple_(HealthScore.recorded_at, HealthScore.id) > (cursor_ts, cursor_id)).order_by(
                    asc(HealthScore.recorded_at), asc(HealthScore.id)
                )
        else:
            query = query.order_by(asc(HealthScore.recorded_at), asc(HealthScore.id))

        rows = query.limit(limit + 1).all()

        return [
            {
                "recovery_date": row.recorded_at.date(),
                "provider": row.provider,
                "source": data_source.source if data_source else None,
                "device_model": data_source.device_model if data_source else None,
                "device_type": data_source.device_type if data_source else None,
                "record_id": row.id,
                "recorded_at": row.recorded_at,
                "recovery_score": int(row.value) if row.value is not None else None,
                "resting_heart_rate": cast(dict, row.components or {}).get("resting_heart_rate", {}).get("value"),
                "hrv_rmssd_milli": cast(dict, row.components or {}).get("hrv_rmssd_milli", {}).get("value"),
                "spo2_percentage": cast(dict, row.components or {}).get("spo2_percentage", {}).get("value"),
            }
            for row, data_source in rows
        ]

    def get_latest_per_category(
        self,
        db_session: DbSession,
        user_id: UUID,
    ) -> list[HealthScore]:
        """Return the most recent score for each category for a given user.

        Uses PostgreSQL DISTINCT ON (category) for efficiency.
        """
        return (
            db_session.query(HealthScore)
            .filter(HealthScore.user_id == user_id)
            .distinct(HealthScore.category)
            .order_by(HealthScore.category, desc(HealthScore.recorded_at))
            .all()
        )
