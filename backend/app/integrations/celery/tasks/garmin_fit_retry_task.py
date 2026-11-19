"""Recover Garmin FIT files after a transient download failure."""

from typing import cast
from uuid import UUID

from celery import shared_task

from app.config import settings
from app.constants.celery_tasks import CeleryTask
from app.database import SessionLocal
from app.services.providers.garmin.data_247 import Garmin247Data, schedule_fit_retry
from app.services.providers.garmin.strategy import GarminStrategy
from app.services.providers.retry import is_transient_provider_error
from app.utils.structured_logging import get_logger

logger = get_logger(__name__)


@shared_task(name=CeleryTask.RETRY_GARMIN_FIT_DOWNLOAD)
def retry_garmin_fit_download(user_id: str, activity_id: str, callback_url: str, attempt: int = 1) -> None:
    try:
        with SessionLocal() as db:
            service = cast("Garmin247Data", GarminStrategy().data_247)
            service.retry_fit_download(db, UUID(user_id), activity_id, callback_url)
            db.commit()
    except Exception as exc:
        retry = is_transient_provider_error(exc) and attempt < settings.garmin_fit_retry_max_attempts
        logger.error(
            "Garmin FIT retry failed",
            exc_info=exc,
            provider="garmin",
            user_id=user_id,
            activity_id=activity_id,
            attempt=attempt,
            retry=retry,
        )
        if retry:
            schedule_fit_retry(user_id, activity_id, callback_url, attempt + 1)
