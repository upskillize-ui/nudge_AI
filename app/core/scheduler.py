import logging
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger
from app.database import SessionLocal
from app.core.engine import NudgeEngine
from app.config import get_settings

log = logging.getLogger("scheduler")
scheduler = BackgroundScheduler()

def _run(fn_name):
    db = SessionLocal()
    try:
        engine = NudgeEngine(db)
        result = getattr(engine, fn_name)()
        if result: log.info(f"{fn_name}: {len(result)} nudges")
    except Exception as e: log.error(f"{fn_name} failed: {e}")
    finally: db.close()

def start_scheduler():
    s = get_settings()
    scheduler.add_job(lambda: _run("check_deadlines"), IntervalTrigger(minutes=s.nudge_check_interval_minutes),
                      id="deadlines", replace_existing=True)
    scheduler.add_job(lambda: _run("check_unwatched_recordings"), IntervalTrigger(minutes=30),
                      id="recordings", replace_existing=True)
    if s.enable_dropout_prediction:
        scheduler.add_job(lambda: _run("predict_dropout"), IntervalTrigger(days=7),
                          id="dropout", replace_existing=True)
    # Expire old nudges hourly
    def expire():
        from datetime import datetime
        from app.models import Nudge
        db = SessionLocal()
        try:
            db.query(Nudge).filter(Nudge.status=="pending", Nudge.expires_at<datetime.utcnow()
                ).update({"status": "expired"}, synchronize_session="fetch")
            db.commit()
        except: pass
        finally: db.close()
    scheduler.add_job(expire, IntervalTrigger(hours=1), id="expire", replace_existing=True)
    scheduler.start()
    log.info(f"Scheduler started: deadlines every {s.nudge_check_interval_minutes}m, recordings every 30m")

def stop_scheduler():
    if scheduler.running: scheduler.shutdown(wait=False)
