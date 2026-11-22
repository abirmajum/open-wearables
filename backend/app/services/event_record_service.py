from collections.abc import Iterable, Sequence
from datetime import date, datetime, time, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from typing import cast
from uuid import UUID, uuid4

from sqlalchemy import Column, Table, and_, delete, exists, func, select, tuple_
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Query

from app.config import settings
from app.database import DbSession
from app.integrations.rollup_queue import mark_dirty_days
from app.models import (
    DataSource,
    DetailType,
    EventRecord,
    EventRecordAggregate,
    EventRecordDetail,
    HealthScore,
    MealDetails,
    MenstrualCycleDetails,
    SeriesSample,
    SleepDetails,
    WorkoutDetails,
)
from app.repositories import (
    AggregateRepository,
    DataPointSeriesRepository,
    DataSourceRepository,
    EventRecordDetailRepository,
    EventRecordRepository,
    HealthScoreRepository,
)
from app.repositories.data_point_series_repository import WorkoutWindow
from app.repositories.data_source_repository import DataSourceIdentity
from app.repositories.data_type_coverage_repository import data_type_coverage_repository
from app.repositories.event_record_repository import local_days
from app.schemas.data_type_coverage import CoverageSpan
from app.schemas.enums import EventCategory, HealthScoreCategory, SeriesType, SourceSelection, get_series_type_unit
from app.schemas.model_crud.activities import (
    EventRecordCreate,
    EventRecordDetailCreate,
    EventRecordQueryParams,
    EventRecordResponse,
    EventRecordUpdate,
    MealDetailCreate,
    MenstrualCycleDetailCreate,
    SleepInclude,
    WorkoutInclude,
)
from app.schemas.model_crud.activities.sleep import SleepStage
from app.schemas.model_crud.activities.zones import HRZones, PowerZones
from app.schemas.responses.activity import (
    ActivityRecord,
    Macros,
    Meal,
    MenstrualCycleRecord,
    NutrientValue,
    SleepSession,
    SleepStagesSummary,
    SleepTotals,
    Workout,
    WorkoutTotals,
)
from app.schemas.sync_status import DataTypeKind
from app.schemas.utils import (
    PaginatedResponse,
    Pagination,
    TimeseriesMetadata,
)
from app.schemas.utils import (
    SourceMetadata as DataSourceSchema,
)
from app.services.outgoing_webhooks import svix as svix_service
from app.services.outgoing_webhooks.events import (
    on_meal_created,
    on_menstrual_cycle_created,
    on_sleep_created,
    on_workout_created,
)
from app.services.priority_service import priority_service
from app.services.scores.sleep_service import sleep_score_service
from app.services.services import AppService
from app.services.sleep_finality import final_record_ids
from app.services.sleep_naps import reclassify_naps
from app.utils.after_commit import run_after_commit
from app.utils.context import current_sync_payload
from app.utils.conversion import as_dict_list, as_float, as_int, as_model, minutes_to_seconds, sum_optional
from app.utils.dates import local_date
from app.utils.pagination import encode_cursor
from app.utils.sleep_stages import measured_stages
from app.utils.structured_logging import get_logger

logger = get_logger(__name__)


def _meal_summary(nutrients: dict[SeriesType, float]) -> tuple[float | None, Macros | None, float | None]:
    """Calories, macros (None when none recorded) and water for a meal, as /events/meals and meal.created show them."""
    macros = Macros(
        protein_g=nutrients.get(SeriesType.dietary_protein),
        carbohydrates_g=nutrients.get(SeriesType.dietary_carbohydrates),
        fat_g=nutrients.get(SeriesType.dietary_fat_total),
        fiber_g=nutrients.get(SeriesType.dietary_fiber),
    )
    has_macros = any(v is not None for v in macros.model_dump().values())
    return (
        nutrients.get(SeriesType.dietary_energy_consumed),
        macros if has_macros else None,
        nutrients.get(SeriesType.hydration),
    )


def _nutrient_values(nutrients: dict[SeriesType, float]) -> dict[str, NutrientValue]:
    return {t.value: NutrientValue(value=v, unit=get_series_type_unit(t)) for t, v in nutrients.items()}


def pace_sec_per_km(distance_meters: float | None, seconds: int | None) -> float | None:
    """Average pace in seconds per kilometre.

    Derived from distance and time rather than ``average_speed``: every provider stores m/s now,
    but a pace from the workout's own distance and time needs no speed at all.
    """
    if distance_meters is None or seconds is None or distance_meters <= 0 or seconds <= 0:
        return None
    return round(seconds / (distance_meters / 1000), 1)


def _first_number(*values: Decimal | None) -> float | None:
    """The first value that is set, so a provider's 0 kcal is kept rather than skipped."""
    return next((float(value) for value in values if value is not None), None)


def _fill_nulls(db_session: DbSession, key: Column, target: UUID, source: UUID, columns: Iterable[str]) -> None:
    """Each of these columns of the ``target`` row that is null takes the ``source`` row's value."""
    table = key.table
    other = table.alias("other")
    db_session.execute(
        table.update()
        .where(key == target)
        .values(
            {
                c: func.coalesce(table.c[c], select(other.c[c]).where(other.c[key.key] == source).scalar_subquery())
                for c in columns
            }
        )
    )


class EventRecordService(
    AppService[EventRecordRepository, EventRecord, EventRecordCreate, EventRecordUpdate],
):
    """Service coordinating CRUD access for unified health records."""

    def __init__(self, **kwargs):
        super().__init__(crud_model=EventRecordRepository, model=EventRecord, **kwargs)
        self.event_record_detail_repo = EventRecordDetailRepository(EventRecordDetail)
        self.aggregate_repo = AggregateRepository(EventRecordAggregate)
        self.data_source_repo = DataSourceRepository()
        self.data_point_series_repo = DataPointSeriesRepository(SeriesSample)
        self.health_score_repo = HealthScoreRepository(HealthScore)

    def _resolve_avg_hr(
        self,
        db_session: DbSession,
        records: list[EventRecord],
        statistics: dict[UUID, dict[str, Decimal | None]],
    ) -> dict[UUID, int | None]:
        """Return avg HR for each workout record.

        Uses the workout's heart rate aggregate (the provider's, else ours from its own samples) where
        available, falls back to a single batch query against series_sample of the other sources of the
        same writer.
        """
        result: dict[UUID, int | None] = {}
        missing = []

        for r in records:
            average = statistics.get(r.id, {}).get("heart_rate_avg")
            if average is not None:
                # Half up, as the SQL fallback below rounds: 72.5 is 73 either way.
                result[r.id] = int(Decimal(average).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
            else:
                result[r.id] = None
                missing.append(r)

        if missing:
            siblings = self.data_source_repo.get_writer_siblings(db_session, {r.data_source_id for r in missing})
            windows = [
                WorkoutWindow(
                    r.id, r.data_source_id, r.start_datetime, r.end_datetime, siblings.get(r.data_source_id, ())
                )
                for r in missing
            ]
            result.update(self.data_point_series_repo.get_avg_hr_for_workout_batch(db_session, windows))

        return result

    def _build_response(
        self,
        record: EventRecord,
        data_source: DataSource,
    ) -> EventRecordResponse:
        return EventRecordResponse(
            id=record.id,
            external_id=record.external_id,
            category=record.category,
            type=record.type,
            source_name=record.source_name,
            duration_seconds=record.duration_seconds,
            start_datetime=record.start_datetime,
            end_datetime=record.end_datetime,
            zone_offset=record.zone_offset,
            data_source_id=record.data_source_id,
            user_id=data_source.user_id,
            source=data_source.source,
        )

    def create_detail(
        self,
        db_session: DbSession,
        detail: EventRecordDetailCreate,
        detail_type: DetailType = EventCategory.WORKOUT,
    ) -> EventRecordDetail:
        result = self.event_record_detail_repo.create(db_session, detail, detail_type=detail_type)
        # event_record_detail_repo.create commits internally, so data is already persisted.
        # Fire the webhook directly with fresh fetches rather than via after_commit — using
        # after_commit defers the call to the *next* session commit, at which point SQLAlchemy
        # has expired the captured ORM objects, causing lazy-load failures inside after_commit.
        record = self.crud.get(db_session, detail.record_id)
        if record is not None and record.data_source_id is not None:
            data_source = self.data_source_repo.get(db_session, record.data_source_id)
            if data_source is not None:
                self._emit_event_record_webhook(record, data_source, detail, current_sync_payload())

        return result

    @staticmethod
    def _to_local(dt: datetime, zone_offset: str | None) -> datetime:
        """Shift a datetime into the session's local zone (mirrors SQL logic in fill task)."""
        dt = dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)
        if zone_offset is not None:
            sign = 1 if zone_offset[0] == "+" else -1
            hours, minutes = int(zone_offset[1:3]), int(zone_offset[4:6])
            dt = dt.astimezone(timezone(timedelta(hours=sign * hours, minutes=sign * minutes)))
        return dt

    @classmethod
    def _local_sleep_date(cls, start_datetime: datetime, zone_offset: str | None) -> date:
        """Return the local calendar date of a sleep session start."""
        return cls._to_local(start_datetime, zone_offset).date()

    def _widen_coverage(self, db_session: DbSession, session: EventRecord) -> None:
        """Widen the provider's coverage of the session's category to a session re-sent over a longer window.

        A shorter one is not narrowed here: that recount takes the global coverage lock, too much for every webhook.
        """
        data_source = self.data_source_repo.get(db_session, session.data_source_id)
        if data_source is None:
            return
        span = CoverageSpan(
            data_source.user_id,
            data_source.provider,
            session.category,
            DataTypeKind.EVENT,
            session.start_datetime,
            session.end_datetime,
        )
        data_type_coverage_repository.record(db_session, [span])

    def _recompute_sleep_scores(
        self,
        db_session: DbSession,
        user_id: UUID,
        sleep_dates: set[date],
    ) -> None:
        """Delete existing internal sleep scores for each date and recompute them immediately.

        Accepts multiple dates so callers can cover both the old and new local
        date when a session shifts across midnight.  The session data has already
        been flushed, so sleep_score_service sees up-to-date rows within the
        same transaction.

        Scores per session rather than per date, matching fill_missing_sleep_scores.
        The two writers have to agree: recorded_at is what the unique constraint keys
        on, so a second convention here produces a duplicate score instead of
        replacing the existing one.
        """
        if not settings.ow_scores_enabled:
            return

        # Widened by a day either side so a session whose local start lands on a target
        # date is still inside the window whatever its zone offset.
        window_start = datetime.combine(min(sleep_dates), time.min, tzinfo=timezone.utc) - timedelta(days=1)
        window_end = datetime.combine(max(sleep_dates), time.min, tzinfo=timezone.utc) + timedelta(days=2)

        scorable = [
            (record, self._to_local(record.end_datetime, record.zone_offset))
            for record, details in self.crud.get_sleep_records_with_details(
                db_session, user_id, window_start, window_end
            )
            if details is not None and not details.is_nap and details.sleep_total_duration_minutes
        ]

        # A session running past midnight is scored on the day it ends, so clear the wake
        # dates of the sessions the caller named as well as the start dates themselves.
        stale_dates = sleep_dates | {
            local_end.date()
            for record, local_end in scorable
            if self._local_sleep_date(record.start_datetime, record.zone_offset) in sleep_dates
        }

        # Rebuild every session scored on a cleared date, not just the caller's — an
        # earlier session ending that morning is scored there too and would otherwise
        # be deleted without being rewritten.
        sessions = [
            (record.id, record.data_source_id, local_end)
            for record, local_end in scorable
            if local_end.date() in stale_dates
        ]

        for d in stale_dates:
            self.health_score_repo.delete_for_user_date(db_session, user_id, d, HealthScoreCategory.SLEEP)

        creators = sleep_score_service.build_internal_sleep_scores(db_session, user_id, sessions)
        if creators:
            self.health_score_repo.bulk_create(db_session, creators)

    def reclassify_sleep_naps(
        self,
        db_session: DbSession,
        user_id: UUID,
        data_source_id: UUID,
        wake_dates: set[date] | None,
    ) -> int:
        """Re-flag a data source's naps around the given local wake dates (its whole history for None).

        A session that became or stopped being a nap is rescored with its day. The caller commits.
        Returns how many flags changed.
        """
        changes = reclassify_naps(db_session, data_source_id, wake_dates)
        if changes:
            # Per day: the score query caps its rows from the window's start, so far-apart days would lose scores
            for wake_date in sorted({change.wake_date for change in changes}):
                self._recompute_sleep_scores(db_session, user_id, {wake_date})
        return len(changes)

    def reclassify_sleep_naps_after_write(self, db_session: DbSession, user_id: UUID, record: EventRecord) -> bool:
        """Re-flag the naps around a stored sleep session and commit; True when a flag changed.

        A night that arrives after a nap makes it one. On failure the session keeps the flag it was stored with,
        and the next write on its days re-flags them.
        """
        record_id, wake_date = record.id, local_date(record.end_datetime, record.zone_offset)
        try:
            if self.reclassify_sleep_naps(db_session, user_id, record.data_source_id, {wake_date}):
                db_session.commit()
                return True
        except Exception as e:
            db_session.rollback()
            logger.error(
                "Nap re-flagging failed after sleep write", exc_info=e, record_id=str(record_id), user_id=str(user_id)
            )
        return False

    def find_adjacent_sleep_record(
        self,
        db_session: DbSession,
        user_id: UUID,
        start_time: datetime,
        end_time: datetime,
        threshold_minutes: int,
        source: str | None = None,
        provider: str | None = None,
        source_app_id: str | None = None,
    ) -> EventRecord | None:
        """Find an existing sleep session adjacent to [start_time, end_time]."""
        return self.crud.find_adjacent_sleep_record(
            db_session,
            user_id,
            start_time,
            end_time,
            threshold_minutes,
            source=source,
            provider=provider,
            source_app_id=source_app_id,
        )

    def upsert_meals(
        self,
        db_session: DbSession,
        meals: list[tuple[EventRecordCreate, MealDetailCreate]],
    ) -> int:
        """Insert or refresh meals with their details; webhooks only for new ones. Returns the number inserted."""
        stored = self.crud.bulk_upsert_meals(db_session, [record for record, _ in meals])
        inserted: list[EventRecordDetailCreate] = []
        refreshed: list[EventRecordDetailCreate] = []
        for record, detail in meals:
            if record.id not in stored:
                continue
            record_id, is_inserted = stored[record.id]
            (inserted if is_inserted else refreshed).append(detail.model_copy(update={"record_id": record_id}))

        if inserted:
            self.bulk_create_details(db_session, inserted, detail_type=EventCategory.MEAL)
        if refreshed:
            self.event_record_detail_repo.bulk_create(db_session, refreshed, detail_type=EventCategory.MEAL)
        return len(inserted)

    def create_or_merge_sleep(
        self,
        db_session: DbSession,
        user_id: UUID,
        record: EventRecordCreate,
        detail: EventRecordDetailCreate,
        threshold_minutes: int,
        *,
        merge_adjacent: bool = True,
    ) -> EventRecord:
        """Create a sleep record, merging with any adjacent session within threshold_minutes.

        When an adjacent session already exists in the database within the gap threshold,
        the two records are merged: the time window expands to cover both, stage minutes
        are summed (or recomputed from the merged stage timeline when windows overlap and
        stages are available), and efficiency is recalculated as a time-in-bed-weighted
        average over sessions that have a non-None score.  The merged record is created
        first, and the old record is deleted only after a successful insert — so a failure
        never loses the original data.
        """
        result, inserted, final_detail = self._create_or_merge_sleep_inner(
            db_session, user_id, record, detail, threshold_minutes, merge_adjacent=merge_adjacent
        )
        is_nap = final_detail.is_nap
        if self.reclassify_sleep_naps_after_write(db_session, user_id, result) and result.sleep_detail is not None:
            is_nap = result.sleep_detail.is_nap
        if inserted:
            eff = final_detail.sleep_efficiency_score
            has_stages = any(
                minutes is not None
                for minutes in (
                    final_detail.sleep_awake_minutes,
                    final_detail.sleep_light_minutes,
                    final_detail.sleep_deep_minutes,
                    final_detail.sleep_rem_minutes,
                )
            )
            device_type = None
            if svix_service.is_enabled() and result.data_source_id is not None:
                data_source = self.data_source_repo.get(db_session, result.data_source_id)
                if data_source is not None:
                    device_type = data_source.device_type

            on_sleep_created(
                record_id=result.id,
                user_id=user_id,
                provider=record.provider or record.source_name,
                device=record.device_model,
                start_time=result.start_datetime.isoformat(),
                end_time=result.end_datetime.isoformat(),
                zone_offset=record.zone_offset,
                duration_seconds=result.duration_seconds,
                efficiency_percent=float(eff) if eff is not None else None,
                stages={
                    "awake_minutes": final_detail.sleep_awake_minutes,
                    "light_minutes": final_detail.sleep_light_minutes,
                    "deep_minutes": final_detail.sleep_deep_minutes,
                    "rem_minutes": final_detail.sleep_rem_minutes,
                }
                if has_stages
                else None,
                is_nap=is_nap,
                source_app=record.source,
                device_type=device_type,
                sleep_duration_seconds=(
                    final_detail.sleep_total_duration_minutes * 60
                    if final_detail.sleep_total_duration_minutes is not None
                    else None
                ),
                sleep_stage_intervals=(
                    [stage.model_dump(mode="json") for stage in final_detail.sleep_stages]
                    if final_detail.sleep_stages
                    else None
                ),
                sync=current_sync_payload(),
            )
        return result

    def refresh_sleep(
        self,
        db_session: DbSession,
        user_id: UUID,
        stored: EventRecord,
        record: EventRecordCreate,
        detail: EventRecordDetailCreate,
    ) -> None:
        """A session the provider sent again under its id: the stored one takes the new span and detail.

        Caller is responsible for commit.
        """
        old_start, old_end, old_zone = stored.start_datetime, stored.end_datetime, stored.zone_offset
        for field in ("start_datetime", "end_datetime", "zone_offset"):
            new_val = getattr(record, field, None)
            if new_val is not None:
                setattr(stored, field, new_val)
        stored.duration_seconds = int((stored.end_datetime - stored.start_datetime).total_seconds())
        if stored.end_datetime != old_end:
            # Its provider score sits at the old end, which a shortened night leaves outside its window.
            self.health_score_repo.drop_provider_sleep_score(db_session, stored.data_source_id, old_end)
        db_session.flush()
        self._widen_coverage(db_session, stored)
        # Its session rows are rolled up over the window it has now, on the days it left and joined.
        mark_dirty_days(
            db_session,
            [
                *local_days(stored.data_source_id, old_start, old_end, old_zone),
                *local_days(stored.data_source_id, stored.start_datetime, stored.end_datetime, stored.zone_offset),
            ],
        )
        self.event_record_detail_repo.delete_by_record_id(db_session, stored.id, EventCategory.SLEEP)
        self.event_record_detail_repo.create_and_flush(
            db_session,
            detail.model_copy(update={"record_id": stored.id}),
            detail_type=EventCategory.SLEEP,
        )
        self._recompute_sleep_scores(
            db_session,
            user_id,
            {
                self._local_sleep_date(old_start, old_zone),
                self._local_sleep_date(stored.start_datetime, stored.zone_offset),
            },
        )

    def lock_workouts(self, db_session: DbSession, user_id: UUID, provider: str, external_ids: Iterable[str]) -> None:
        self.crud.lock_workouts(db_session, user_id, provider, external_ids)

    def lock_stored_workouts(
        self, db_session: DbSession, user_id: UUID, provider: str, external_ids: Iterable[str]
    ) -> dict[str, EventRecord]:
        """Lock these workout ids of the user until the transaction ends, and return the stored workout of each.

        A workout stored twice, as two concurrent pushes could before the lock, is merged into its copy under the
        device first (see :meth:`_merge_workout_copies`).
        """
        ids = list(external_ids)
        self.lock_workouts(db_session, user_id, provider, ids)
        stored: dict[str, EventRecord] = {}
        for external_id, copies in self.crud.get_copies_by_external_ids(
            db_session, user_id, ids, provider, EventCategory.WORKOUT
        ).items():
            stored[external_id] = self._merge_workout_copies(db_session, user_id, provider, copies)
        return stored

    def _merge_workout_copies(
        self, db_session: DbSession, user_id: UUID, provider: str, copies: list[tuple[EventRecord, str | None]]
    ) -> EventRecord:
        """The one workout these copies of an id are: each copy under no device is merged into the one copy under a
        device, a value either holds kept and the device's kept where both do, and then deleted. A statistic both hold
        keeps the larger count.

        Copies under two devices, or under none, are no copy of a FIT file and its summary, so all are kept and the
        first stands for the workout. Coverage is not recounted, as when a push shortens a stored workout. The recount
        would wait on the global coverage lock that WHOOP syncs take, with this workout still locked.
        """
        target = copies[0][0]
        if len(copies) == 1:
            return target
        record_ids = [str(record.id) for record, _ in copies]
        devices = [record for record, device_model in copies if device_model is not None]
        if len(devices) != 1:
            logger.warning(
                "Workout stored more than once, copies not merged",
                provider=provider,
                user_id=str(user_id),
                external_id=target.external_id,
                record_ids=record_ids,
            )
            return target
        db_session.flush()
        records = cast(Table, EventRecord.__table__)
        details = cast(Table, WorkoutDetails.__table__)
        stats = cast(Table, EventRecordAggregate.__table__)
        days = local_days(target.data_source_id, target.start_datetime, target.end_datetime, target.zone_offset)
        for copy, _ in copies[1:]:
            _fill_nulls(db_session, records.c.id, target.id, copy.id, ("type", "subtype", "zone_offset"))
            other_details = details.alias("other_details")
            db_session.execute(
                details.update()
                .where(details.c.record_id == copy.id, ~exists().where(other_details.c.record_id == target.id))
                .values(record_id=target.id)
            )
            _fill_nulls(
                db_session,
                details.c.record_id,
                target.id,
                copy.id,
                [c.key for c in details.columns if c.key not in ("record_id", "created_at")],
            )
            other_stats = stats.alias("other_stats")
            same_series = and_(
                other_stats.c.series_type_definition_id == stats.c.series_type_definition_id,
                other_stats.c.computed_by == stats.c.computed_by,
            )
            db_session.execute(
                stats.update()
                .where(stats.c.record_id == target.id, other_stats.c.record_id == copy.id, same_series)
                .values(
                    {c: func.coalesce(stats.c[c], other_stats.c[c]) for c in ("sum", "avg", "min", "max", "last")}
                    | {"count": func.greatest(stats.c.count, other_stats.c.count)}
                )
            )
            db_session.execute(
                stats.update()
                .where(stats.c.record_id == copy.id, ~exists().where(other_stats.c.record_id == target.id, same_series))
                .values(record_id=target.id)
            )
            days += local_days(copy.data_source_id, copy.start_datetime, copy.end_datetime, copy.zone_offset)
            db_session.execute(delete(EventRecord).where(EventRecord.id == copy.id))
            logger.warning(
                "Workout stored twice, merged into its device's copy and the other copy removed",
                provider=provider,
                user_id=str(user_id),
                external_id=target.external_id,
                record_id=str(target.id),
                removed_record_id=str(copy.id),
            )
        db_session.expire(target)
        mark_dirty_days(db_session, days)
        return target

    def update_stored_workouts(
        self,
        db_session: DbSession,
        user_id: UUID,
        provider: str,
        workouts: Sequence[tuple[EventRecordCreate, EventRecordDetailCreate]],
    ) -> tuple[list[tuple[EventRecordCreate, EventRecordDetailCreate]], list[UUID]]:
        """Update the user's stored workouts that these pushes send again under their id.

        A provider can send one workout in several pushes, in any order, each with some of its fields. The stored
        workout takes the device, type and span a push names, and its detail the values the push carries; what a
        push leaves out keeps its stored value. Caller is responsible for commit; the workouts' ids stay locked until
        then, so a concurrent push of one of them waits and finds what this one stores.

        Returns:
            The workouts not stored yet, and the ids of the stored ones updated.
        """
        stored = self.lock_stored_workouts(
            db_session, user_id, provider, (record.external_id or "" for record, _ in workouts)
        )
        new: list[tuple[EventRecordCreate, EventRecordDetailCreate]] = []
        updated: list[UUID] = []
        for record, detail in workouts:
            if (target := stored.get(record.external_id or "")) is None:
                new.append((record, detail))
                continue
            self._refresh_workout(db_session, target, record)
            # One upsert per detail: a batch carrying the same workout twice would update one row twice
            self.event_record_detail_repo.bulk_create(
                db_session, [detail.model_copy(update={"record_id": target.id})], detail_type=EventCategory.WORKOUT
            )
            updated.append(target.id)
        return new, updated

    def save_workouts(
        self,
        db_session: DbSession,
        user_id: UUID,
        provider: str,
        workouts: Sequence[tuple[EventRecordCreate, EventRecordDetailCreate]],
    ) -> tuple[list[UUID], list[UUID]]:
        """Store these workouts of the user: one stored under its id takes what the push carries, the rest are
        inserted. Caller is responsible for commit, which sends the new workouts' webhooks.

        Returns:
            The ids of the workouts inserted, and of the stored ones updated.
        """
        if not workouts:
            return [], []
        new, updated = self.update_stored_workouts(db_session, user_id, provider, workouts)
        first_inserted: set[UUID] = set()
        fallback_inserted: set[UUID] = set()
        candidates = [record for record, _ in new]
        inserted_this_round = first_inserted
        while candidates:
            first_deliveries = []
            later_deliveries = []
            seen: set[str] = set()
            for record in candidates:
                if record.external_id:
                    if record.external_id in seen:
                        later_deliveries.append(record)
                        continue
                    seen.add(record.external_id)
                first_deliveries.append(record)
            stored = self.bulk_create(db_session, first_deliveries)
            inserted_this_round.update(record_id for record_id, stored_id in stored.items() if record_id == stored_id)
            stored_keys = {record.external_id for record in first_deliveries if record.id in stored}
            candidates = [record for record in later_deliveries if record.external_id not in stored_keys]
            inserted_this_round = fallback_inserted
        inserted = first_inserted | fallback_inserted
        self.bulk_create_details(db_session, [detail for record, detail in new if record.id in first_inserted])
        # If an earlier span was occupied, replay details in delivery order onto the later insert.
        if repeated := [(record, detail) for record, detail in new if record.id not in first_inserted]:
            dropped, again = self.update_stored_workouts(db_session, user_id, provider, repeated)
            updated.extend(record_id for record_id in again if record_id not in fallback_inserted)
            if dropped:
                logger.warning(
                    "Workouts not stored, other records of their sources hold their spans",
                    provider=provider,
                    user_id=str(user_id),
                    held_by=self._span_holders(db_session, [record for record, _ in dropped]),
                )
        if fallback_inserted and svix_service.is_enabled():
            self._queue_bulk_webhooks(
                db_session,
                [
                    EventRecordDetailCreate.model_validate(detail, from_attributes=True)
                    for record_id in fallback_inserted
                    if (
                        detail := self.event_record_detail_repo.get_by_record_id(
                            db_session, record_id, EventCategory.WORKOUT
                        )
                    )
                    is not None
                ],
            )
        return [record.id for record, _ in new if record.id in inserted], updated

    def _span_holders(self, db_session: DbSession, records: list[EventRecordCreate]) -> dict[str, str]:
        """The id of the stored record holding each record's span in its data source, by the record's external id."""
        sources = self.data_source_repo.resolve_creators(db_session, records)
        spans = {
            (sources.get(DataSourceIdentity.of(record)), record.start_datetime, record.end_datetime): str(
                record.external_id
            )
            for record in records
        }
        held = db_session.execute(
            select(EventRecord.id, EventRecord.data_source_id, EventRecord.start_datetime, EventRecord.end_datetime)
            .where(tuple_(EventRecord.data_source_id, EventRecord.start_datetime, EventRecord.end_datetime).in_(spans))
            .where(EventRecord.category != EventCategory.MEAL)
        )
        return {spans[(source_id, start, end)]: str(record_id) for record_id, source_id, start, end in held}

    def _refresh_workout(self, db_session: DbSession, stored: EventRecord, record: EventRecordCreate) -> None:
        """The stored workout takes the device, type and span the push names."""
        old_source, old_start, old_end, old_zone = (
            stored.data_source_id,
            stored.start_datetime,
            stored.end_datetime,
            stored.zone_offset,
        )
        changes = {
            field: value
            for field in ("type", "duration_seconds", "start_datetime", "end_datetime", "zone_offset")
            if (value := getattr(record, field)) is not None
        }
        if record.device_model is not None and (
            source_id := self.data_source_repo.resolve_creators(db_session, [record]).get(DataSourceIdentity.of(record))
        ):
            changes |= {"data_source_id": source_id, "source_name": record.source_name}
        try:
            with db_session.begin_nested():
                for field, value in changes.items():
                    setattr(stored, field, value)
                db_session.flush()
        except IntegrityError as e:
            logger.warning(
                "Re-sent workout not moved, another record holds its source and span",
                record_id=str(stored.id),
                external_id=stored.external_id,
                error=str(e),
            )
            return
        self._widen_coverage(db_session, stored)
        mark_dirty_days(
            db_session,
            [
                *local_days(old_source, old_start, old_end, old_zone),
                *local_days(stored.data_source_id, stored.start_datetime, stored.end_datetime, stored.zone_offset),
            ],
        )

    def _create_or_merge_sleep_inner(
        self,
        db_session: DbSession,
        user_id: UUID,
        record: EventRecordCreate,
        detail: EventRecordDetailCreate,
        threshold_minutes: int,
        *,
        merge_adjacent: bool = True,
    ) -> tuple[EventRecord, bool, EventRecordDetailCreate]:
        # External identity is stronger than temporal adjacency. A provider may
        # correct a session's start/end by more than the merge threshold.
        adjacent = (
            self.crud.get_by_external_id(
                db_session,
                user_id,
                record.external_id,
                source=record.source,
                provider=record.provider,
            )
            if record.external_id is not None
            else None
        )
        if adjacent is None and merge_adjacent:
            adjacent = self.find_adjacent_sleep_record(
                db_session,
                user_id,
                record.start_datetime,
                record.end_datetime,
                threshold_minutes,
                source=record.source,
                provider=record.provider,
            )

        if adjacent is not None:
            # Same external_id → re-ingestion of the same session (e.g. webhook
            # retry, score update).  Replace the detail with fresh values instead
            # of accumulating them on top of the existing ones.
            if record.external_id is not None and adjacent.external_id == record.external_id:
                self.refresh_sleep(db_session, user_id, adjacent, record, detail)
                db_session.commit()
                return adjacent, False, detail

            adj_detail: SleepDetails | None = adjacent.sleep_detail

            def _adj_opt_int(attr: str) -> int | None:
                return getattr(adj_detail, attr) if adj_detail else None

            def _adj_int(attr: str) -> int:
                return _adj_opt_int(attr) or 0

            adj_in_bed = _adj_int("sleep_time_in_bed_minutes")
            new_in_bed = detail.sleep_time_in_bed_minutes or 0
            merged_in_bed = adj_in_bed + new_in_bed

            # Weighted efficiency: only include sessions that have a non-None score
            # so a missing score on one session does not dilute the merged average.
            merged_efficiency: Decimal | None = None
            eff_numerator = 0.0
            eff_denominator = 0
            if adj_detail and adj_detail.sleep_efficiency_score and adj_in_bed > 0:
                eff_numerator += float(adj_detail.sleep_efficiency_score) * adj_in_bed
                eff_denominator += adj_in_bed
            if detail.sleep_efficiency_score and new_in_bed > 0:
                eff_numerator += float(detail.sleep_efficiency_score) * new_in_bed
                eff_denominator += new_in_bed
            if eff_denominator > 0:
                merged_efficiency = Decimal(str(round(eff_numerator / eff_denominator, 2)))

            # Merge sleep_stages: convert DB dicts back to SleepStage, concatenate, sort
            adj_stages_raw = (adj_detail.sleep_stages if adj_detail else None) or []
            adj_stages = [SleepStage.model_validate(s) for s in adj_stages_raw]
            new_stages = list(detail.sleep_stages or [])
            merged_stages: list[SleepStage] | None = None
            if adj_stages or new_stages:
                merged_stages = sorted(adj_stages + new_stages, key=lambda s: s.start_time)

            merged_start = min(adjacent.start_datetime, record.start_datetime)
            merged_end = max(adjacent.end_datetime, record.end_datetime)

            # Compute per-stage minute totals.  When the windows actually overlap
            # and stage intervals are available, recompute from the merged timeline
            # (clipping overlaps — consistent with Apple SDK's _calculate_final_metrics)
            # to avoid double-counting the overlapping period.  For non-overlapping
            # sessions simple summation is exact.
            overlap_seconds = max(
                0,
                (
                    min(adjacent.end_datetime, record.end_datetime)
                    - max(adjacent.start_datetime, record.start_datetime)
                ).total_seconds(),
            )
            if overlap_seconds > 0 and merged_stages:
                deep_secs = light_secs = rem_secs = awake_secs = sleeping_secs = 0.0
                last_end = None
                for s in merged_stages:  # already sorted above
                    s_start, s_end = s.start_time, s.end_time
                    if last_end is not None and s_start < last_end:
                        s_start = last_end
                    if s_start >= s_end:
                        continue
                    dur = (s_end - s_start).total_seconds()
                    stage_str = str(s.stage)
                    if stage_str == "deep":
                        deep_secs += dur
                    elif stage_str == "light":
                        light_secs += dur
                    elif stage_str == "rem":
                        rem_secs += dur
                    elif stage_str == "awake":
                        awake_secs += dur
                    elif stage_str == "sleeping":
                        sleeping_secs += dur
                    last_end = s_end
                deep_min, light_min, rem_min = int(deep_secs / 60), int(light_secs / 60), int(rem_secs / 60)
                merged_total = deep_min + light_min + rem_min + int(sleeping_secs / 60)
                # A merged hypnogram of only sleeping segments still reports no phases
                has_phases, has_awake = measured_stages(merged_stages)
                merged_deep = deep_min if has_phases else None
                merged_light = light_min if has_phases else None
                merged_rem = rem_min if has_phases else None
                merged_awake = int(awake_secs / 60) if has_awake else None
            else:
                # A side without stages adds nothing; the other side's minutes are kept
                merged_deep = sum_optional(_adj_opt_int("sleep_deep_minutes"), detail.sleep_deep_minutes)
                merged_light = sum_optional(_adj_opt_int("sleep_light_minutes"), detail.sleep_light_minutes)
                merged_rem = sum_optional(_adj_opt_int("sleep_rem_minutes"), detail.sleep_rem_minutes)
                merged_awake = sum_optional(_adj_opt_int("sleep_awake_minutes"), detail.sleep_awake_minutes)
                merged_total = sum_optional(
                    _adj_opt_int("sleep_total_duration_minutes"), detail.sleep_total_duration_minutes
                )

            logger.info(
                "Merging adjacent sleep records",
                provider=record.provider,
                user_id=str(user_id),
                adjacent_record_id=str(adjacent.id),
                record_id=str(record.id),
            )

            merged_detail_fields = {
                "sleep_deep_minutes": merged_deep,
                "sleep_light_minutes": merged_light,
                "sleep_rem_minutes": merged_rem,
                "sleep_awake_minutes": merged_awake,
                "sleep_total_duration_minutes": merged_total,
                "sleep_time_in_bed_minutes": merged_in_bed,
                "sleep_efficiency_score": merged_efficiency,
                "is_nap": bool(adj_detail.is_nap if adj_detail else False) and bool(detail.is_nap or False),
                "sleep_stages": merged_stages,
            }

            # When the merged window is identical to the existing record's window
            # (new session fully contained within adjacent), inserting a new record
            # would violate the unique constraint on (data_source_id, start, end).
            # Detect this upfront and update the detail in-place instead.
            same_window = (
                merged_start == adjacent.start_datetime
                and merged_end == adjacent.end_datetime
                and record.data_source_id is not None
                and record.data_source_id == adjacent.data_source_id
            )

            if same_window:
                self.event_record_detail_repo.delete_by_record_id(db_session, adjacent.id, EventCategory.SLEEP)
                self.event_record_detail_repo.create_and_flush(
                    db_session,
                    detail.model_copy(update={"record_id": adjacent.id, **merged_detail_fields}),
                    detail_type=EventCategory.SLEEP,
                )
                self._recompute_sleep_scores(
                    db_session,
                    user_id,
                    {self._local_sleep_date(adjacent.start_datetime, adjacent.zone_offset)},
                )
                db_session.commit()
                return adjacent, False, detail

            record = record.model_copy(
                update={
                    "id": uuid4(),
                    "start_datetime": merged_start,
                    "end_datetime": merged_end,
                    "duration_seconds": int((merged_end - merged_start).total_seconds()),
                }
            )
            created_record = self.crud.create_and_flush(db_session, record)

            if created_record.id == adjacent.id:
                # data_source_id was None (resolved at insert time) and the
                # constraint returned the existing row — treat as same_window.
                self.event_record_detail_repo.delete_by_record_id(db_session, adjacent.id, EventCategory.SLEEP)
                self.event_record_detail_repo.create_and_flush(
                    db_session,
                    detail.model_copy(update={"record_id": adjacent.id, **merged_detail_fields}),
                    detail_type=EventCategory.SLEEP,
                )
                self._recompute_sleep_scores(
                    db_session,
                    user_id,
                    {self._local_sleep_date(adjacent.start_datetime, adjacent.zone_offset)},
                )
                db_session.commit()
                return adjacent, False, detail

            merged_final_detail = detail.model_copy(update={"record_id": created_record.id, **merged_detail_fields})
            self.event_record_detail_repo.create_and_flush(
                db_session,
                merged_final_detail,
                detail_type=EventCategory.SLEEP,
            )
            adj_start, adj_zone = adjacent.start_datetime, adjacent.zone_offset
            # The first segment's session values (the provider's lowest heart rate, our rollup) would go with
            # its record; they move to the merged night, where the next segment's values combine with them.
            self.aggregate_repo.move_record(db_session, adjacent.id, created_record.id)
            self.crud.delete_flush(db_session, adjacent)
            self._recompute_sleep_scores(
                db_session,
                user_id,
                {
                    self._local_sleep_date(adj_start, adj_zone),
                    self._local_sleep_date(created_record.start_datetime, created_record.zone_offset),
                },
            )
            db_session.commit()
            return created_record, True, merged_final_detail

        created_record = self.crud.create_and_flush(db_session, record)
        new_detail = detail.model_copy(update={"record_id": created_record.id})
        self.event_record_detail_repo.create_and_flush(
            db_session,
            new_detail,
            detail_type=EventCategory.SLEEP,
        )
        db_session.commit()
        return created_record, True, new_detail

    @staticmethod
    def _emit_event_record_webhook(
        record: EventRecord,
        data_source: DataSource,
        detail: EventRecordDetailCreate,
        sync: dict[str, str] | None = None,
    ) -> None:
        """Fire the appropriate outgoing webhook for a newly created event record."""
        if not svix_service.is_enabled():
            return
        category = (record.category or "").lower()
        provider = str(data_source.provider)
        device = data_source.device_model
        zone_offset = record.zone_offset
        match category:
            case EventCategory.SLEEP:
                eff = detail.sleep_efficiency_score
                has_stages = any(
                    minutes is not None
                    for minutes in (
                        detail.sleep_awake_minutes,
                        detail.sleep_light_minutes,
                        detail.sleep_deep_minutes,
                        detail.sleep_rem_minutes,
                    )
                )
                on_sleep_created(
                    record_id=record.id,
                    user_id=data_source.user_id,
                    provider=provider,
                    device=device,
                    start_time=record.start_datetime.isoformat(),
                    end_time=record.end_datetime.isoformat(),
                    zone_offset=zone_offset,
                    duration_seconds=record.duration_seconds,
                    efficiency_percent=float(eff) if eff is not None else None,
                    stages={
                        "awake_minutes": detail.sleep_awake_minutes,
                        "light_minutes": detail.sleep_light_minutes,
                        "deep_minutes": detail.sleep_deep_minutes,
                        "rem_minutes": detail.sleep_rem_minutes,
                    }
                    if has_stages
                    else None,
                    is_nap=detail.is_nap,
                    source_app=data_source.source,
                    device_type=data_source.device_type,
                    sleep_duration_seconds=(
                        detail.sleep_total_duration_minutes * 60
                        if detail.sleep_total_duration_minutes is not None
                        else None
                    ),
                    sleep_stage_intervals=(
                        [stage.model_dump(mode="json") for stage in detail.sleep_stages]
                        if detail.sleep_stages
                        else None
                    ),
                    sync=sync,
                )
            case EventCategory.MENSTRUAL_CYCLE:
                mcd = detail if isinstance(detail, MenstrualCycleDetailCreate) else None
                on_menstrual_cycle_created(
                    record_id=record.id,
                    user_id=data_source.user_id,
                    provider=provider,
                    device=device,
                    start_time=record.start_datetime.isoformat(),
                    end_time=record.end_datetime.isoformat(),
                    zone_offset=zone_offset,
                    current_phase_type=mcd.current_phase_type if mcd else None,
                    day_in_cycle=mcd.day_in_cycle if mcd else None,
                    cycle_length=mcd.cycle_length if mcd else None,
                    is_predicted_cycle=mcd.is_predicted_cycle if mcd else None,
                    pregnancy_snapshot=mcd.pregnancy_snapshot if mcd else None,
                    sync=sync,
                )
            case EventCategory.WORKOUT:
                avg_pace = pace_sec_per_km(
                    as_float(detail.distance),
                    detail.moving_time_seconds if detail.moving_time_seconds is not None else record.duration_seconds,
                )
                on_workout_created(
                    record_id=record.id,
                    user_id=data_source.user_id,
                    provider=provider,
                    device=device,
                    workout_type=record.type,
                    start_time=record.start_datetime.isoformat(),
                    end_time=record.end_datetime.isoformat(),
                    zone_offset=zone_offset,
                    duration_seconds=record.duration_seconds,
                    calories_kcal=float(detail.energy_burned) if detail.energy_burned is not None else None,
                    distance_meters=float(detail.distance) if detail.distance is not None else None,
                    avg_heart_rate_bpm=int(detail.heart_rate_avg) if detail.heart_rate_avg is not None else None,
                    max_heart_rate_bpm=int(detail.heart_rate_max) if detail.heart_rate_max is not None else None,
                    elevation_gain_meters=float(detail.total_elevation_gain)
                    if detail.total_elevation_gain is not None
                    else None,
                    avg_pace_sec_per_km=round(avg_pace) if avg_pace is not None else None,
                    sync=sync,
                )
            case EventCategory.MEAL if isinstance(detail, MealDetailCreate):
                calories_kcal, macros, water_ml = _meal_summary(detail.nutrients)
                on_meal_created(
                    record_id=record.id,
                    user_id=data_source.user_id,
                    provider=provider,
                    device=device,
                    start_time=record.start_datetime.isoformat(),
                    end_time=record.end_datetime.isoformat(),
                    zone_offset=zone_offset,
                    title=detail.title,
                    meal_type=detail.meal_type,
                    calories_kcal=calories_kcal,
                    macros=macros.model_dump() if macros else None,
                    water_ml=water_ml,
                    nutrients={code: value.model_dump() for code, value in _nutrient_values(detail.nutrients).items()},
                    sync=sync,
                )

    def bulk_create(
        self,
        db_session: DbSession,
        records: list[EventRecordCreate],
    ) -> dict[UUID, UUID]:
        """Bulk create event records with batch data source resolution."""
        # Webhooks are not emitted for bulk-created records; they fire from
        # bulk_create_details() when the matching details are inserted.
        return self.crud.bulk_create(db_session, records)

    def bulk_create_details(
        self,
        db_session: DbSession,
        details: list[EventRecordDetailCreate],
        detail_type: DetailType = EventCategory.WORKOUT,
        created: set[UUID] | None = None,
    ) -> None:
        """Bulk create event record details and fire one webhook per detail on commit.

        With created given, only those records fire it; the rest are refreshes of stored records.
        """
        # Several deliveries of one workout in a batch resolve to one record; the last one's detail stands
        details = list({d.record_id: d for d in details}.values())
        self.event_record_detail_repo.bulk_create(db_session, details, detail_type=detail_type)

        if created is not None:
            details = [d for d in details if d.record_id in created]
        self._queue_bulk_webhooks(db_session, details)

    def _queue_bulk_webhooks(self, db_session: DbSession, details: list[EventRecordDetailCreate]) -> None:
        if not details or not svix_service.is_enabled():
            return

        record_ids = [d.record_id for d in details if d.record_id is not None]
        if not record_ids:
            return

        records = self.crud.get_by_ids(db_session, record_ids)
        records_by_id = {r.id: r for r in records}

        data_source_ids = {r.data_source_id for r in records if r.data_source_id is not None}
        data_sources = self.data_source_repo.get_by_ids(db_session, data_source_ids)
        data_sources_by_id = {ds.id: ds for ds in data_sources}

        dispatches: list[tuple[EventRecord, DataSource, EventRecordDetailCreate]] = []
        for detail in details:
            record = records_by_id.get(detail.record_id)
            if record is None or record.data_source_id is None:
                continue
            data_source = data_sources_by_id.get(record.data_source_id)
            if data_source is None:
                continue
            dispatches.append((record, data_source, detail))

        if not dispatches:
            return

        # Detach so expire_on_commit doesn't expire these; reading expired attributes
        # inside after_commit (committed session) would raise. Only loaded scalars are
        # read, and these objects are local (callers get only IDs), so detaching is safe.
        for record in records:
            db_session.expunge(record)
        for data_source in data_sources:
            db_session.expunge(data_source)

        sync = current_sync_payload()

        def _dispatch_bulk_webhooks() -> None:
            for record, data_source, detail in dispatches:
                self._emit_event_record_webhook(record, data_source, detail, sync)

        run_after_commit(db_session, _dispatch_bulk_webhooks)

    def _get_records_with_filters(
        self,
        db_session: DbSession,
        query_params: EventRecordQueryParams,
        user_id: str,
        restrict_to_record_ids: Query | None = None,
    ) -> tuple[list[tuple[EventRecord, DataSource]], int]:
        logger.debug("Fetching event records", user_id=user_id, filters=query_params.model_dump(mode="json"))

        records, total_count = self.crud.get_records_with_filters(
            db_session, query_params, user_id, restrict_to_record_ids=restrict_to_record_ids
        )

        logger.debug("Retrieved event records", user_id=user_id, count=len(records), total_count=total_count)

        return records, total_count

    @staticmethod
    def _page(
        records: list[tuple[EventRecord, DataSource]], params: EventRecordQueryParams
    ) -> tuple[list[tuple[EventRecord, DataSource]], bool, str | None, str | None]:
        """The page of records fetched one past the limit, whether more follow, and its next and previous cursors.

        A backward page keeps the records nearest its cursor and has a previous cursor only while more precede it.
        """
        limit = params.limit or 20
        has_more = len(records) > limit
        is_backward = bool(params.cursor and params.cursor.startswith("prev_"))
        if has_more:
            records = records[-limit:] if is_backward else records[:limit]
        if not records:
            return records, has_more, None, None
        first, last = records[0][0], records[-1][0]
        next_cursor = encode_cursor(last.start_datetime, last.id, "next") if has_more else None
        previous_cursor = (
            encode_cursor(first.start_datetime, first.id, "prev")
            if params.cursor and (has_more or not is_backward)
            else None
        )
        return records, has_more, next_cursor, previous_cursor

    def get_records_response(
        self,
        db_session: DbSession,
        query_params: EventRecordQueryParams,
        user_id: str,
    ) -> list[EventRecordResponse]:
        records, _ = self._get_records_with_filters(db_session, query_params, user_id)

        return [self._build_response(record, data_source) for record, data_source in records]

    def get_workout_types(self, db_session: DbSession, user_id: UUID) -> list[str]:
        """Workout types this user actually has, so a client can filter on real options only."""
        return self.crud.get_distinct_workout_types(db_session, user_id)

    def get_category_counts(self, db_session: DbSession) -> list[tuple[str, int]]:
        """Count event records grouped by category (cheap aggregate on a small table)."""
        return self.crud.get_category_counts(db_session)

    def _map_source(self, data_source: DataSource) -> DataSourceSchema:
        return DataSourceSchema(
            provider=data_source.provider or "unknown",
            source=data_source.source,
            device=data_source.device_model,
            device_type=data_source.device_type,
            device_id=data_source.device_id,
        )

    def get_workout_totals(
        self,
        db_session: DbSession,
        user_id: UUID,
        params: EventRecordQueryParams,
        source_selection: SourceSelection = SourceSelection.ALL,
    ) -> WorkoutTotals:
        restrict = self._winning_session_ids(db_session, user_id, params, EventCategory.WORKOUT, source_selection)
        count, seconds, energy, distance = self.crud.get_workout_totals(
            db_session, params, str(user_id), restrict_to_record_ids=restrict
        )
        return WorkoutTotals(
            count=count,
            duration_seconds=seconds,
            calories_kcal=float(energy) if energy is not None else None,
            distance_meters=float(distance) if distance is not None else None,
        )

    def get_workouts(
        self,
        db_session: DbSession,
        user_id: UUID,
        params: EventRecordQueryParams,
        include: Sequence[WorkoutInclude] = (),
        source_selection: SourceSelection = SourceSelection.ALL,
    ) -> PaginatedResponse[Workout]:
        params.category = EventCategory.WORKOUT
        with_zones = WorkoutInclude.ZONES in include
        with_segments = WorkoutInclude.SEGMENTS in include
        restrict = self._winning_session_ids(db_session, user_id, params, EventCategory.WORKOUT, source_selection)
        records, total_count = self._get_records_with_filters(
            db_session, params, str(user_id), restrict_to_record_ids=restrict
        )
        # Ensure total_count is always an int (not None)
        total_count = total_count if total_count is not None else 0

        records, has_more, next_cursor, previous_cursor = self._page(records, params)

        statistics = self.crud.get_workout_statistics(db_session, [r.id for r, _ in records])
        computed_hr = self._resolve_avg_hr(db_session, [r for r, _ in records], statistics)

        data = []
        for record, data_source in records:
            details: WorkoutDetails | None = record.workout_detail
            stats = statistics.get(record.id, {})
            calories = _first_number(
                stats.get("provider_energy"), details.energy_burned if details else None, stats.get("own_energy")
            )
            moving_seconds = (
                details.moving_time_seconds
                if details and details.moving_time_seconds is not None
                else record.duration_seconds
            )
            workout = Workout(
                id=record.id,
                type=record.type or "unknown",
                name=details.label if details else None,
                start_time=record.start_datetime,
                end_time=record.end_datetime,
                zone_offset=record.zone_offset,
                duration_seconds=record.duration_seconds,
                source=self._map_source(data_source),
                entry_source=details.entry_source if details else None,
                intensity=details.intensity if details else None,
                calories_kcal=calories,
                distance_meters=as_float(stats.get("distance")),
                avg_heart_rate_bpm=computed_hr.get(record.id),
                max_heart_rate_bpm=as_int(stats.get("heart_rate_max")),
                avg_pace_sec_per_km=pace_sec_per_km(as_float(stats.get("distance")), moving_seconds),
                elevation_gain_meters=as_float(stats.get("total_elevation_gain")),
                heart_rate_min=as_int(stats.get("heart_rate_min")),
                steps_count=as_int(stats.get("steps_count")),
                average_speed=as_float(stats.get("average_speed")),
                max_speed=as_float(stats.get("max_speed")),
                average_cadence=as_float(stats.get("average_cadence")),
                average_watts=as_float(stats.get("average_watts")),
                max_watts=as_float(stats.get("max_watts")),
                average_met=as_float(stats.get("average_met")),
                moving_time_seconds=details.moving_time_seconds if details else None,
                elev_high=as_float(stats.get("elev_high")),
                elev_low=as_float(stats.get("elev_low")),
                aerobic_training_effect=as_float(details.aerobic_training_effect) if details else None,
                anaerobic_training_effect=as_float(details.anaerobic_training_effect) if details else None,
                training_load=as_float(details.training_load) if details else None,
                hr_zones=as_model(HRZones, details.hr_zones) if details and with_zones else None,
                power_zones=as_model(PowerZones, details.power_zones) if details and with_zones else None,
                segments=as_dict_list(details.segments) if details and with_segments else None,
            )
            data.append(workout)

        return PaginatedResponse(
            data=data,
            pagination=Pagination(
                has_more=has_more,
                next_cursor=next_cursor,
                previous_cursor=previous_cursor,
                total_count=total_count,
            ),
            metadata=TimeseriesMetadata(
                sample_count=len(data),
                start_time=params.start_datetime,
                end_time=params.end_datetime,
            ),
        )

    def _winning_session_ids(
        self,
        db_session: DbSession,
        user_id: UUID,
        params: EventRecordQueryParams,
        category: EventCategory,
        source_selection: SourceSelection,
    ) -> Query | None:
        """An inline subquery of the sessions no better-ranked source also recorded, None when every source shows."""
        # By value: a caller passing the plain string "per_session" takes this path too.
        if source_selection != SourceSelection.PER_SESSION:
            return None
        params = params.model_copy(update={"category": category})
        return self.crud.winning_session_record_ids(
            db_session, str(user_id), params, priority_service.get_source_ranking(db_session)
        )

    def get_sleep_totals(
        self,
        db_session: DbSession,
        user_id: UUID,
        params: EventRecordQueryParams,
        source_selection: SourceSelection = SourceSelection.ALL,
    ) -> SleepTotals:
        restrict = self._winning_session_ids(db_session, user_id, params, EventCategory.SLEEP, source_selection)
        count, naps, asleep, in_bed, efficiency = self.crud.get_sleep_totals(
            db_session, params, str(user_id), restrict_to_record_ids=restrict
        )
        return SleepTotals(
            count=count,
            naps=naps,
            sleep_duration_seconds=asleep,
            time_in_bed_seconds=in_bed,
            avg_efficiency_percent=float(efficiency) if efficiency is not None else None,
        )

    def get_sleep_sessions(
        self,
        db_session: DbSession,
        user_id: UUID,
        params: EventRecordQueryParams,
        source_selection: SourceSelection = SourceSelection.ALL,
        include: Sequence[SleepInclude] = (),
    ) -> PaginatedResponse[SleepSession]:
        params.category = EventCategory.SLEEP
        with_stages = SleepInclude.STAGES in include
        restrict_to_record_ids = self._winning_session_ids(
            db_session, user_id, params, EventCategory.SLEEP, source_selection
        )

        records, total_count = self._get_records_with_filters(
            db_session, params, str(user_id), restrict_to_record_ids=restrict_to_record_ids
        )
        # Ensure total_count is always an int (not None)
        total_count = total_count if total_count is not None else 0

        records, has_more, next_cursor, previous_cursor = self._page(records, params)

        final_ids = final_record_ids(db_session, user_id, records, datetime.now(timezone.utc))
        data = []
        for record, data_source in records:
            details: SleepDetails | None = record.sleep_detail

            sleep_duration_seconds = minutes_to_seconds(details.sleep_total_duration_minutes) if details else None
            time_in_bed_seconds = minutes_to_seconds(details.sleep_time_in_bed_minutes) if details else None
            session = SleepSession(
                id=record.id,
                start_time=record.start_datetime,
                end_time=record.end_datetime,
                zone_offset=record.zone_offset,
                source=self._map_source(data_source),
                duration_seconds=record.duration_seconds or 0,
                sleep_duration_seconds=sleep_duration_seconds,
                time_in_bed_seconds=time_in_bed_seconds,
                efficiency_percent=as_float(details.sleep_efficiency_score) if details else None,
                is_nap=details.is_nap if (details and details.is_nap is not None) else False,
                is_final=record.id in final_ids,
                sleep_stage_intervals=details.sleep_stages if details and with_stages else None,
                stages=SleepStagesSummary(
                    deep_minutes=details.sleep_deep_minutes,
                    light_minutes=details.sleep_light_minutes,
                    rem_minutes=details.sleep_rem_minutes,
                    awake_minutes=details.sleep_awake_minutes,
                )
                if details
                else None,
            )
            data.append(session)

        return PaginatedResponse(
            data=data,
            pagination=Pagination(
                has_more=has_more,
                next_cursor=next_cursor,
                previous_cursor=previous_cursor,
                total_count=total_count,
            ),
            metadata=TimeseriesMetadata(
                sample_count=len(data),
                start_time=params.start_datetime,
                end_time=params.end_datetime,
            ),
        )

    def get_menstrual_cycles(
        self,
        db_session: DbSession,
        user_id: UUID,
        params: EventRecordQueryParams,
    ) -> PaginatedResponse[MenstrualCycleRecord]:
        params.category = EventCategory.MENSTRUAL_CYCLE
        # Cycles can end in the future (predicted end of cycle), so filtering by
        # end_datetime would exclude current/upcoming cycles. Filter by start_datetime only.
        params.end_datetime = None
        records, total_count = self._get_records_with_filters(db_session, params, str(user_id))
        total_count = total_count if total_count is not None else 0

        records, has_more, next_cursor, previous_cursor = self._page(records, params)

        data = []
        for record, data_source in records:
            details: MenstrualCycleDetails | None = record.menstrual_cycle_detail
            data.append(
                MenstrualCycleRecord(
                    id=record.id,
                    start_time=record.start_datetime,
                    end_time=record.end_datetime,
                    zone_offset=record.zone_offset,
                    source=self._map_source(data_source),
                    current_phase=details.current_phase if details else None,
                    current_phase_type=details.current_phase_type if details else None,
                    day_in_cycle=details.day_in_cycle if details else None,
                    cycle_length=details.cycle_length if details else None,
                    predicted_cycle_length=details.predicted_cycle_length if details else None,
                    is_predicted_cycle=details.is_predicted_cycle if details else None,
                    period_length=details.period_length if details else None,
                    length_of_current_phase=details.length_of_current_phase if details else None,
                    days_until_next_phase=details.days_until_next_phase if details else None,
                    fertile_window_start=details.fertile_window_start if details else None,
                    length_of_fertile_window=details.length_of_fertile_window if details else None,
                    last_updated_at=details.last_updated_at if details else None,
                    has_specified_cycle_length=details.has_specified_cycle_length if details else None,
                    has_specified_period_length=details.has_specified_period_length if details else None,
                    pregnancy_snapshot=details.pregnancy_snapshot if details else None,
                )
            )

        return PaginatedResponse(
            data=data,
            pagination=Pagination(
                has_more=has_more,
                next_cursor=next_cursor,
                previous_cursor=previous_cursor,
                total_count=total_count,
            ),
            metadata=TimeseriesMetadata(
                sample_count=len(data),
                start_time=params.start_datetime,
                end_time=params.end_datetime,
            ),
        )

    def get_activities(
        self,
        db_session: DbSession,
        user_id: UUID,
        params: EventRecordQueryParams,
    ) -> PaginatedResponse[ActivityRecord]:
        params.category = EventCategory.ACTIVITY
        records, total_count = self._get_records_with_filters(db_session, params, str(user_id))

        records, has_more, next_cursor, previous_cursor = self._page(records, params)

        data = [
            ActivityRecord(
                id=record.id,
                type=record.type,
                subtype=record.subtype,
                start_time=record.start_datetime,
                end_time=record.end_datetime,
                zone_offset=record.zone_offset,
                duration_seconds=record.duration_seconds,
                source=self._map_source(data_source),
            )
            for record, data_source in records
        ]

        return PaginatedResponse(
            data=data,
            pagination=Pagination(
                has_more=has_more,
                next_cursor=next_cursor,
                previous_cursor=previous_cursor,
                total_count=total_count,
            ),
            metadata=TimeseriesMetadata(
                sample_count=len(data),
                start_time=params.start_datetime,
                end_time=params.end_datetime,
            ),
        )

    def get_meals(
        self,
        db_session: DbSession,
        user_id: UUID,
        params: EventRecordQueryParams,
    ) -> PaginatedResponse[Meal]:
        params.category = EventCategory.MEAL
        records, total_count = self._get_records_with_filters(db_session, params, str(user_id))

        records, has_more, next_cursor, previous_cursor = self._page(records, params)

        data = []
        for record, data_source in records:
            details: MealDetails | None = record.meal_detail
            nutrients = {
                SeriesType(code): value
                for code, value in (details.nutrients if details else {}).items()
                if code in SeriesType.__members__
            }
            calories_kcal, macros, water_ml = _meal_summary(nutrients)
            data.append(
                Meal(
                    id=record.id,
                    timestamp=record.start_datetime,
                    meal_type=details.meal_type if details else None,
                    name=details.title if details else None,
                    source=self._map_source(data_source),
                    calories_kcal=calories_kcal,
                    macros=macros,
                    water_ml=water_ml,
                    nutrients=_nutrient_values(nutrients),
                )
            )

        return PaginatedResponse(
            data=data,
            pagination=Pagination(
                has_more=has_more,
                next_cursor=next_cursor,
                previous_cursor=previous_cursor,
                total_count=total_count,
            ),
            metadata=TimeseriesMetadata(
                sample_count=len(data),
                start_time=params.start_datetime,
                end_time=params.end_datetime,
            ),
        )

    def delete_event_record(
        self,
        db_session: DbSession,
        user_id: UUID,
        record_id: UUID,
        category: EventCategory,
    ) -> bool:
        """Delete an event record by id and category. Returns False if not found or not owned by user."""
        record = self.crud.get_record_with_details(db_session, record_id, category)
        if not record:
            return False
        data_source = self.data_source_repo.get(db_session, record.data_source_id)
        if not data_source or data_source.user_id != user_id:
            return False
        # Read before the delete: without the night, a nap of that day may be its main sleep now
        source_id, wake_date = record.data_source_id, local_date(record.end_datetime, record.zone_offset)
        self.crud.delete_flush(db_session, record)
        data_type_coverage_repository.refresh_events(
            db_session, [(data_source.user_id, data_source.provider, record.category)]
        )
        if category == EventCategory.SLEEP:
            self.reclassify_sleep_naps(db_session, user_id, source_id, {wake_date})
        db_session.commit()
        return True


event_record_service = EventRecordService()
