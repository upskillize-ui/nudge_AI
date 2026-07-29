"""Background jobs.

APScheduler runs in-process, once per worker. With more than one uvicorn
worker every job fires N times and students get duplicate nudges — the
Dockerfile pins --workers 1 for exactly this reason.
"""
import logging
import os
from datetime import datetime

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from app.config import get_settings
from app.database import SessionLocal
from app.models import Nudge
from app.services.activities import ActivityService
from app.services.assignments import AssignmentService
from app.services.dropout import DropoutService
from app.services.recordings import RecordingService
from app.services.sessions import SessionService

log = logging.getLogger("scheduler")
scheduler = BackgroundScheduler(timezone="Asia/Kolkata")

#: Hour (IST) at which the nightly feature aggregation runs.
FEATURE_AGGREGATION_HOUR = 2

#: How often overdue recordings are swept, in minutes.
RECORDING_SWEEP_MINUTES = 30

#: How often upcoming classes are checked. Must be well under the tightest
#: reminder tier (15 minutes) or the tier is missed entirely.
CLASS_REMINDER_MINUTES = 5

#: How often abandoned attempts are swept.
ACTIVITY_SWEEP_MINUTES = 15

#: How often expired pending nudges are retired, in hours.
EXPIRY_SWEEP_HOURS = 1

#: How often the dropout model scores students, in days.
DROPOUT_PREDICTION_DAYS = 7


def _run(label: str, work) -> None:
    """Run one job inside its own session, logging rather than raising.

    A scheduler job that raises kills nothing but its own run, and the
    traceback would be swallowed by APScheduler — so failures are logged
    explicitly here.

    Args:
        label: Job name for log lines.
        work: Callable taking a Session and returning a result to summarise.
    """
    db = SessionLocal()
    try:
        result = work(db)
        if result is not None:
            size = len(result) if hasattr(result, "__len__") else result
            log.info("%s: %s", label, size)
    except Exception as exc:  # noqa: BLE001 — job isolation is the point
        log.error("%s failed: %s", label, exc)
    finally:
        db.close()


def check_deadlines() -> None:
    """Chase students whose coursework deadlines are approaching."""
    _run("deadlines", lambda db: AssignmentService(db).check_deadlines())


def check_recordings() -> None:
    """Chase students who have not watched overdue recordings."""
    _run("recordings", lambda db: RecordingService(db).check_unwatched())


def send_class_reminders() -> None:
    """Remind students about classes starting in 60 / 30 / 15 minutes."""
    _run("class_reminders", lambda db: SessionService(db).send_reminders())


def sweep_abandoned() -> None:
    """Chase activities a student started and left unfinished."""
    _run("abandoned", lambda db: ActivityService(db).sweep())


def aggregate_features() -> None:
    """Refresh the ML feature table from the tracker tables."""
    _run("features", lambda db: DropoutService(db).aggregate_features())


def predict_dropout() -> None:
    """Score unlabelled students and alert mentors on high risk."""
    _run("dropout", lambda db: DropoutService(db).predict())


def expire_stale_nudges() -> None:
    """Retire pending nudges that have passed their expiry."""
    def work(db):
        count = db.query(Nudge).filter(
            Nudge.status == "pending",
            Nudge.expires_at < datetime.utcnow(),
        ).update({"status": "expired"}, synchronize_session="fetch")
        db.commit()
        return count or None
    _run("expire", work)


def start_scheduler() -> None:
    """Register every background job and start the scheduler."""
    settings = get_settings()

    workers = int(os.environ.get("WEB_CONCURRENCY", "1"))
    if workers > 1:
        log.warning(
            "WEB_CONCURRENCY=%d — APScheduler fires each job once per worker, "
            "which produces DUPLICATE NUDGES. Run with --workers 1 or move the "
            "scheduler to a separate process.", workers,
        )

    scheduler.add_job(
        check_deadlines,
        IntervalTrigger(minutes=settings.nudge_check_interval_minutes),
        id="deadlines", replace_existing=True,
    )
    scheduler.add_job(
        check_recordings,
        IntervalTrigger(minutes=RECORDING_SWEEP_MINUTES),
        id="recordings", replace_existing=True,
    )
    scheduler.add_job(
        send_class_reminders,
        IntervalTrigger(minutes=CLASS_REMINDER_MINUTES),
        id="class_reminders", replace_existing=True,
    )
    scheduler.add_job(
        sweep_abandoned,
        IntervalTrigger(minutes=ACTIVITY_SWEEP_MINUTES),
        id="abandoned", replace_existing=True,
    )
    scheduler.add_job(
        aggregate_features,
        CronTrigger(hour=FEATURE_AGGREGATION_HOUR, minute=0),
        id="agg_features", replace_existing=True,
    )
    scheduler.add_job(
        expire_stale_nudges,
        IntervalTrigger(hours=EXPIRY_SWEEP_HOURS),
        id="expire", replace_existing=True,
    )
    if settings.enable_dropout_prediction:
        scheduler.add_job(
            predict_dropout,
            IntervalTrigger(days=DROPOUT_PREDICTION_DAYS),
            id="dropout", replace_existing=True,
        )

    scheduler.start()
    log.info(
        "Scheduler started: deadlines/%dm, recordings/%dm, classes/%dm, "
        "abandoned/%dm, features daily %02d:00 IST",
        settings.nudge_check_interval_minutes, RECORDING_SWEEP_MINUTES,
        CLASS_REMINDER_MINUTES, ACTIVITY_SWEEP_MINUTES, FEATURE_AGGREGATION_HOUR,
    )


def stop_scheduler() -> None:
    """Shut the scheduler down without waiting for running jobs."""
    if scheduler.running:
        scheduler.shutdown(wait=False)
