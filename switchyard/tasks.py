from celery import Celery

from . import usage, webhooks
from .config import get_settings
from .db import SessionLocal
from .redis_client import get_redis

settings = get_settings()

celery_app = Celery("switchyard", broker=settings.redis_url)
celery_app.conf.update(
    task_ignore_result=True,
    # Only ack after the task finishes, so a worker crash puts it back on the queue.
    task_acks_late=True,
    worker_prefetch_multiplier=1,
    beat_schedule={
        "flush-usage": {"task": "switchyard.flush_usage", "schedule": 60.0},
        "requeue-due-deliveries": {"task": "switchyard.requeue_due", "schedule": 30.0},
    },
)


@celery_app.task(name="switchyard.deliver")
def deliver(delivery_id: int) -> str:
    with SessionLocal() as session:
        status, delay = webhooks.attempt(session, delivery_id)
    if delay is not None:
        deliver.apply_async((delivery_id,), countdown=delay.total_seconds())
    return status


@celery_app.task(name="switchyard.flush_usage")
def flush_usage() -> int:
    with SessionLocal() as session:
        return usage.flush(get_redis(), session)


@celery_app.task(name="switchyard.requeue_due")
def requeue_due() -> int:
    with SessionLocal() as session:
        due = webhooks.find_due(session)
    for delivery_id in due:
        deliver.delay(delivery_id)
    return len(due)
