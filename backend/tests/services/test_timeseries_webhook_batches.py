"""Outgoing time-series webhooks announce only samples that were committed.

Regression: bulk_create_samples registered an after_commit listener per call. When
the transaction rolled back, the listener survived and the next commit announced
the rolled-back samples. All data is synthetic.
"""

from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any
from unittest.mock import patch
from uuid import uuid4

from sqlalchemy.orm import Session

from app.schemas.enums import SeriesType
from app.schemas.model_crud.activities import TimeSeriesSampleCreate
from app.services.timeseries_service import TimeSeriesService, timeseries_service
from tests.factories import UserFactory


class _InlineThread:
    """Runs the webhook thread's target immediately so the test can observe it."""

    def __init__(self, target: Callable[[], Any], daemon: bool) -> None:  # noqa: ARG002
        self.target = target

    def start(self) -> None:
        self.target()


def _sample(user_id: Any, day: int, kcal: float) -> TimeSeriesSampleCreate:
    return TimeSeriesSampleCreate(
        id=uuid4(),
        user_id=user_id,
        source="whoop",
        recorded_at=datetime(2026, 10, day, 21, 0, tzinfo=timezone.utc),
        value=kcal,
        series_type=SeriesType.energy,
        is_daily_total=True,
    )


def test_rolled_back_samples_are_not_announced_on_the_next_commit(db: Session) -> None:
    user = UserFactory()
    db.commit()
    emitted: list[list[float]] = []

    def record(samples: list[TimeSeriesSampleCreate]) -> None:
        emitted.append([float(s.value) for s in samples])

    with (
        patch("app.services.outgoing_webhooks.svix.is_enabled", return_value=True),
        patch("app.services.timeseries_service.threading.Thread", _InlineThread),
        patch.object(TimeSeriesService, "_emit_timeseries_webhooks", side_effect=record),
    ):
        timeseries_service.bulk_create_samples(db, [_sample(user.id, 10, 1000.0)])
        db.rollback()
        timeseries_service.bulk_create_samples(db, [_sample(user.id, 11, 2000.0)])
        timeseries_service.bulk_create_samples(db, [_sample(user.id, 12, 3000.0)])
        db.commit()
        db.commit()  # nothing new: no second announcement

    assert emitted == [[2000.0], [3000.0]]
