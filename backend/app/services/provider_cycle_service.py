import base64
import binascii
import json
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from app.database import DbSession
from app.models.provider_cycle import ProviderCycle
from app.repositories.data_point_series_repository import WriteCounts
from app.repositories.provider_cycle_repository import ProviderCycleRepository
from app.schemas.responses.provider_cycle import CycleSummary
from app.schemas.utils import PaginatedResponse, Pagination, TimeseriesMetadata
from app.utils.exceptions import InvalidCursorError, handle_exceptions


class ProviderCycleService:
    def __init__(self) -> None:
        self.repository = ProviderCycleRepository()

    def save_whoop(self, db: DbSession, user_id: UUID, raw: dict[str, Any]) -> WriteCounts:
        score = (raw.get("score") or {}) if raw.get("score_state") == "SCORED" else {}
        energy = score.get("kilojoule")
        summary = CycleSummary(
            id=str(raw["id"]),
            start_time=raw["start"],
            end_time=raw.get("end"),
            updated_at=raw["updated_at"],
            fetched_at=datetime.now(timezone.utc),
            score_state=raw["score_state"],
            zone_offset=raw.get("timezone_offset"),
            steps=raw.get("step_count"),
            strain=score.get("strain"),
            energy_kj=energy,
            total_calories_kcal=energy / 4.184 if energy is not None else None,
            avg_heart_rate_bpm=score.get("average_heart_rate"),
            max_heart_rate_bpm=score.get("max_heart_rate"),
        )
        return self.repository.upsert(
            db,
            ProviderCycle(
                id=summary.id,
                user_id=user_id,
                provider="whoop",
                updated_at=summary.updated_at,
                fetched_at=summary.fetched_at,
                payload=summary.model_dump(mode="json"),
            ),
        )

    @handle_exceptions
    def page(
        self, db: DbSession, user_id: UUID, start: datetime, end: datetime, cursor: str | None, limit: int
    ) -> PaginatedResponse[CycleSummary]:
        after = None
        if cursor:
            try:
                if len(cursor) > 512:
                    raise ValueError("cursor too long")
                stamp, cycle_id = json.loads(base64.urlsafe_b64decode(cursor))
                if not isinstance(stamp, str) or not isinstance(cycle_id, str) or len(cycle_id) > 64:
                    raise ValueError("invalid cursor fields")
                after = (datetime.fromisoformat(stamp), cycle_id)
                if after[0].tzinfo is None:
                    raise ValueError("cursor needs timezone")
            except (ValueError, TypeError, binascii.Error):
                raise InvalidCursorError(cursor=cursor) from None
        rows = self.repository.page(db, user_id, start, end, limit, after)
        has_more = len(rows) > limit
        rows = rows[:limit]
        next_cursor = None
        if has_more:
            last = rows[-1]
            next_cursor = base64.urlsafe_b64encode(json.dumps([last.updated_at.isoformat(), last.id]).encode()).decode()
        return PaginatedResponse(
            data=[CycleSummary.model_validate(row.payload) for row in rows],
            pagination=Pagination(has_more=has_more, next_cursor=next_cursor, previous_cursor=None),
            metadata=TimeseriesMetadata(start_time=start, end_time=end),
        )


provider_cycle_service = ProviderCycleService()
