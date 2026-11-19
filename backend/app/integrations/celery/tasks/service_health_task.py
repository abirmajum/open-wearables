"""Beat-to-worker probes: only write expiring service-health keys, never user data."""

from celery import shared_task

from app.constants.celery_tasks import CeleryTask
from app.utils.service_health import record_queue_heartbeat


@shared_task(name=CeleryTask.SERVICE_HEARTBEAT, ignore_result=True, time_limit=10)
def service_heartbeat(queue: str) -> None:
    record_queue_heartbeat(queue)
