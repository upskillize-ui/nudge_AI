"""
PATCH NOTES (v2.1):
- Daily 'aggregate_features_daily' job at 02:00 IST so the dropout model
  trains on fresh feature aggregates (was: features stuck at 0 forever).
- Scheduler logs a fat warning if WEB_CONCURRENCY > 1 (every job would
  fire once per worker and cause duplicate nudges). Run with --workers 1
  or move scheduler into a separate process.
- Expire job logs failures instead of silent pass.
"""
import os
import logging
from datetime import datetime
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger
from apscheduler.triggers.cron import CronTrigger

from app.database import SessionLocal
from app.core.engine import NudgeEngine
from app.config import get_settings

log = logging.getLogger("scheduler")
scheduler = BackgroundScheduler(timezone="Asia/Kolkata")


def _run(fn_name):
    db = SessionLocal()
    try:
        engine = NudgeEngine(db)
        result = getattr(engine, fn_name)()
        if result is not None:
            try:
                log.info(f"{fn_name}: {len(result)} items")
            except TypeError:
                log.info(f"{fn_name}: {result}")
    except Exception as e:
        log.error(f"{fn_name} failed: {e}")
    finally:
        db.close()


def _expire():
    from app.models import Nudge
    db = SessionLocal()
    try:
        cnt = db.query(Nudge).filter(
            Nudge.status == "pending",
            Nudge.expires_at < datetime.utcnow()
        ).update({"status": "expired"}, synchronize_session="fetch")
        db.commit()
        if cnt:
            log.info(f"Expired {cnt} stale pending nudges")
    except Exception as e:
        log.error(f"Expire job failed: {e}")
        db.rollback()
    finally:
        db.close()


def start_scheduler():
    s = get_settings()

    # CRITICAL: BackgroundScheduler runs once per worker process. With
    # uvicorn --workers >1 every cron fires N times -> duplicate nudges.
    workers = int(os.environ.get("WEB_CONCURRENCY", "1"))
    if workers > 1:
        log.warning(
            f"⚠ WEB_CONCURRENCY={workers}. APScheduler will fire each job once "
            f"per worker -> DUPLICATE NUDGES. Run with --workers 1 or move "
            f"scheduler to a separate process."
        )

    scheduler.add_job(
        lambda: _run("check_deadlines"),
        IntervalTrigger(minutes=s.nudge_check_interval_minutes),
        id="deadlines", replace_existing=True,
    )
    scheduler.add_job(
        lambda: _run("check_unwatched_recordings"),
        IntervalTrigger(minutes=30),
        id="recordings", replace_existing=True,
    )
    # Daily feature aggregator at 02:00 IST. Without this the ML features
    # stay at 0 and the dropout model is useless.
    scheduler.add_job(
        lambda: _run("aggregate_features_daily"),
        CronTrigger(hour=2, minute=0),
        id="agg_features", replace_existing=True,
    )
    if s.enable_dropout_prediction:
        scheduler.add_job(
            lambda: _run("predict_dropout"),
            IntervalTrigger(days=7),
            id="dropout", replace_existing=True,
        )
    scheduler.add_job(_expire, IntervalTrigger(hours=1),
                      id="expire", replace_existing=True)
    scheduler.start()
    log.info(
        f"Scheduler started: deadlines/{s.nudge_check_interval_minutes}m, "
        f"recordings/30m, features/daily 02:00 IST"
    )


def stop_scheduler():
    if scheduler.running:
        scheduler.shutdown(wait=False)