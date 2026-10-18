import logging

from celery import Celery, signals
from redis import Redis

from app.core.config import settings
from app.core.logging import configure_logging
from app.db.session import session_factory
from app.services.imports import cleanup_orphans
from app.services.processing import discover, run_job

celery = Celery("importguard", broker=settings().redis_url)
celery.conf.update(
    worker_hijack_root_logger=False,
    task_ignore_result=True,
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    worker_prefetch_multiplier=1,
    broker_connection_retry_on_startup=True,
    broker_transport_options={"visibility_timeout": 300},
    beat_schedule={
        "discover-postgres-jobs": {"task": "importguard.dispatch", "schedule": 5.0},
        "clean-orphaned-uploads": {"task": "importguard.cleanup", "schedule": 3600.0},
    },
)


@signals.setup_logging.connect
def setup_celery_logging(**kwargs):
    configure_logging()


@celery.task(name="importguard.process")
def process(job_id):
    run_job(job_id)


@celery.task(name="importguard.dispatch")
def dispatch():
    with session_factory()() as db:
        pending = discover(db)
    for job_id in pending:
        try:
            process.apply_async(args=[str(job_id)], expires=60, retry=False)
        except Exception:
            logging.getLogger(__name__).exception(
                "broker_unavailable", extra={"job_id": str(job_id)}
            )
            break  # PostgreSQL work remains pending; a later discovery retries it.


@celery.task(name="importguard.cleanup")
def cleanup():
    with session_factory()() as db:
        return cleanup_orphans(db)


def broker_ready():
    return Redis.from_url(settings().redis_url, socket_connect_timeout=2, socket_timeout=2).ping()
