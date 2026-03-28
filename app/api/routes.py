"""All API endpoints: webhooks (LMS->Agent), feeds (Agent->LMS), admin/mentor."""
import logging
from datetime import datetime, timedelta
from fastapi import APIRouter, Depends, Query, Header, HTTPException
from sqlalchemy.orm import Session
from sqlalchemy import func
import hmac

from app.database import get_db
from app.config import get_settings
from app.core.engine import NudgeEngine
from app.models import Nudge, NudgeEvent, AttendanceTracker, RecordingTracker, AssignmentTracker, StudentFeatures
from app.schemas import *

log = logging.getLogger("api")
settings = get_settings()

def verify_webhook(x_webhook_secret: str = Header(..., alias="X-Webhook-Secret")):
    if not hmac.compare_digest(x_webhook_secret, settings.lms_webhook_secret):
        raise HTTPException(401, "Invalid webhook secret")

def verify_api(x_api_key: str = Header(..., alias="X-API-Key")):
    if not hmac.compare_digest(x_api_key, settings.api_secret_key):
        raise HTTPException(401, "Invalid API key")

# ============ WEBHOOKS (LMS sends events here) ============
wh = APIRouter(prefix="/webhook", tags=["Webhooks"])

@wh.post("/lecture-attendance")
def wh_attendance(e: LectureAttendanceEvent, _=Depends(verify_webhook), db: Session=Depends(get_db)):
    try:
        n = NudgeEngine(db).process_attendance(e.user_id, e.course_id, e.batch_id, e.attended,
            e.lecture_title, e.mentor_id, e.student_name)
        return {"ok": True, "nudge_id": n.id if n else None}
    except Exception as ex:
        log.error(f"Attendance webhook failed: {ex}")
        db.rollback()
        return {"ok": False, "error": str(ex)}

@wh.post("/recording-uploaded")
def wh_recording_upload(e: RecordingUploadEvent, _=Depends(verify_webhook), db: Session=Depends(get_db)):
    try:
        NudgeEngine(db).register_recording(e.lecture_id, e.course_id, e.batch_id, e.lecture_title,
            e.recording_url, e.uploaded_at, e.expected_by, e.student_ids)
        return {"ok": True, "tracked": len(e.student_ids)}
    except Exception as ex:
        log.error(f"Recording upload webhook failed: {ex}")
        db.rollback()
        return {"ok": False, "error": str(ex)}

@wh.post("/recording-watched")
def wh_recording_watch(e: RecordingWatchEvent, _=Depends(verify_webhook), db: Session=Depends(get_db)):
    try:
        NudgeEngine(db).update_watch_progress(e.user_id, e.lecture_id, e.watch_percent)
        return {"ok": True}
    except Exception as ex:
        log.error(f"Recording watch webhook failed: {ex}")
        db.rollback()
        return {"ok": False, "error": str(ex)}

@wh.post("/assignment-uploaded")
def wh_asgn_upload(e: AssignmentUploadEvent, _=Depends(verify_webhook), db: Session=Depends(get_db)):
    try:
        NudgeEngine(db).register_assignment(e.assignment_id, e.course_id, e.title, e.deadline,
            e.student_ids, e.assignment_type, e.closes_after_deadline)
        return {"ok": True, "tracked": len(e.student_ids)}
    except Exception as ex:
        log.error(f"Assignment upload webhook failed: {ex}")
        db.rollback()
        return {"ok": False, "error": str(ex)}

@wh.post("/assignment-viewed")
def wh_asgn_view(e: AssignmentViewEvent, _=Depends(verify_webhook), db: Session=Depends(get_db)):
    try:
        NudgeEngine(db).mark_viewed(e.assignment_id, e.user_id)
        return {"ok": True}
    except Exception as ex:
        log.error(f"Assignment view webhook failed: {ex}")
        db.rollback()
        return {"ok": False, "error": str(ex)}

@wh.post("/assignment-submitted")
def wh_asgn_submit(e: AssignmentSubmitEvent, _=Depends(verify_webhook), db: Session=Depends(get_db)):
    try:
        NudgeEngine(db).mark_submitted(e.assignment_id, e.user_id)
        return {"ok": True}
    except Exception as ex:
        log.error(f"Assignment submit webhook failed: {ex}")
        db.rollback()
        return {"ok": False, "error": str(ex)}

@wh.post("/quiz-scored")
def wh_quiz(e: QuizScoreEvent, _=Depends(verify_webhook), db: Session=Depends(get_db)):
    try:
        n = NudgeEngine(db).process_quiz(e.user_id, e.course_id, e.topic_name, e.score,
            e.batch_average, e.student_name, e.mentor_id)
        return {"ok": True, "nudge_id": n.id if n else None}
    except Exception as ex:
        log.error(f"Quiz webhook failed: {ex}")
        db.rollback()
        return {"ok": False, "error": str(ex)}

@wh.post("/login")
def wh_login(e: LoginEvent, _=Depends(verify_webhook), db: Session=Depends(get_db)):
    try:
        f = db.query(StudentFeatures).filter(StudentFeatures.user_id==e.user_id,
            StudentFeatures.course_id==e.course_id).first()
        if not f:
            f = StudentFeatures(user_id=e.user_id, course_id=e.course_id,
                login_frequency=0, avg_session_minutes=0, score_trend=0,
                consecutive_misses=0, assignment_completion_rate=0,
                recording_completion_rate=0, days_since_last_login=0,
                total_nudges_received=0, nudge_response_rate=0)
            db.add(f)
        f.login_frequency = (f.login_frequency or 0) + 0.14
        f.avg_session_minutes = ((f.avg_session_minutes or 0) + e.session_minutes) / 2
        f.days_since_last_login = 0
        db.commit()
        return {"ok": True}
    except Exception as ex:
        log.error(f"Login webhook failed: {ex}")
        db.rollback()
        return {"ok": False, "error": str(ex)}

# ============ NUDGE FEED (Agent -> LMS frontend) ============
nf = APIRouter(prefix="/nudges", tags=["Nudge Feed"])

@nf.get("/feed")
def get_feed(user_id: str=Query(...), role: str=Query("student"), status: str=Query("unread"),
             limit: int=Query(20), _=Depends(verify_api), db: Session=Depends(get_db)):
    q = db.query(Nudge).filter(Nudge.user_id==user_id, Nudge.user_role==role)
    if status == "unread": q = q.filter(Nudge.status.in_(["pending", "delivered"]))
    q = q.filter((Nudge.expires_at > datetime.utcnow()) | (Nudge.expires_at.is_(None)))
    unread = db.query(func.count(Nudge.id)).filter(Nudge.user_id==user_id, Nudge.user_role==role,
        Nudge.status.in_(["pending", "delivered"])).scalar() or 0
    nudges = q.order_by(Nudge.created_at.desc()).limit(limit).all()
    for n in nudges:
        if n.status == "pending": n.status = "delivered"; n.delivered_at = datetime.utcnow()
    db.commit()
    return {"nudges": [{"id": n.id, "type": n.nudge_type, "priority": n.priority, "title": n.title,
        "body": n.body, "cta_text": n.cta_text, "cta_url": n.cta_url, "severity": n.severity,
        "status": n.status, "created_at": str(n.created_at), "meta": n.metadata_json} for n in nudges],
        "total_unread": unread}

@nf.get("/unread-count")
def unread_count(user_id: str=Query(...), role: str=Query("student"),
                 _=Depends(verify_api), db: Session=Depends(get_db)):
    total = db.query(func.count(Nudge.id)).filter(Nudge.user_id==user_id, Nudge.user_role==role,
        Nudge.status.in_(["pending", "delivered"])).scalar() or 0
    by_sev = {s: c for s, c in db.query(Nudge.severity, func.count(Nudge.id)).filter(
        Nudge.user_id==user_id, Nudge.status.in_(["pending","delivered"])).group_by(Nudge.severity).all()}
    return {"total": total, "by_severity": by_sev}

@nf.patch("/{nudge_id}/status")
def update_status(nudge_id: str, u: StatusUpdate, _=Depends(verify_api), db: Session=Depends(get_db)):
    n = db.query(Nudge).filter(Nudge.id==nudge_id).first()
    if not n: return {"error": "not found"}
    n.status = u.status; now = datetime.utcnow()
    if u.status == "read": n.read_at = now
    elif u.status == "clicked": n.clicked_at = now
    elif u.status == "dismissed": n.dismissed_at = now
    db.add(NudgeEvent(nudge_id=nudge_id, user_id=n.user_id, event_type=u.status, channel=n.channel))
    db.commit()
    return {"ok": True, "status": u.status}

@nf.post("/batch-read")
def batch_read(user_id: str=Query(...), _=Depends(verify_api), db: Session=Depends(get_db)):
    cnt = db.query(Nudge).filter(Nudge.user_id==user_id, Nudge.status.in_(["pending","delivered"])
        ).update({"status": "read", "read_at": datetime.utcnow()}, synchronize_session="fetch")
    db.commit()
    return {"updated": cnt}

# ============ MENTOR / ADMIN ============
ma = APIRouter(tags=["Mentor & Admin"])

@ma.get("/mentor/critical-students")
def critical_students(batch_id: str=Query(""), _=Depends(verify_api), db: Session=Depends(get_db)):
    return {"students": NudgeEngine(db).get_critical_students(batch_id)}

@ma.get("/student/improvements")
def improvements(user_id: str=Query(...), _=Depends(verify_api), db: Session=Depends(get_db)):
    return NudgeEngine(db).get_student_improvements(user_id)

@ma.get("/student/assignments")
def assignments(user_id: str=Query(...), _=Depends(verify_api), db: Session=Depends(get_db)):
    now = datetime.utcnow()
    trackers = db.query(AssignmentTracker).filter(AssignmentTracker.user_id==user_id).order_by(AssignmentTracker.deadline).all()
    return {"assignments": [{"id": t.assignment_id, "title": t.title, "type": t.assignment_type,
        "deadline": str(t.deadline), "viewed": t.first_viewed_at is not None,
        "status": t.submission_status or "not_started",
        "hours_left": max(0, round((t.deadline-now).total_seconds()/3600, 1)),
        "urgency": "critical" if (t.deadline-now).total_seconds()/3600 < 24 and (t.submission_status or "not_started") != "submitted" else "normal"
    } for t in trackers]}

@ma.get("/reports/attendance")
def attendance_report(course_id: str=Query(""), batch_id: str=Query(""),
                      _=Depends(verify_api), db: Session=Depends(get_db)):
    return {"report": NudgeEngine(db).get_attendance_report(course_id, batch_id)}

@ma.get("/reports/recordings")
def recording_report(course_id: str=Query(""), batch_id: str=Query(""),
                     _=Depends(verify_api), db: Session=Depends(get_db)):
    return {"report": NudgeEngine(db).get_recording_report(course_id, batch_id)}

@ma.get("/admin/analytics")
def analytics(period: str=Query("30d"), _=Depends(verify_api), db: Session=Depends(get_db)):
    days = int(period.replace("d",""))
    since = datetime.utcnow() - timedelta(days=days)
    total = db.query(func.count(Nudge.id)).filter(Nudge.created_at>=since).scalar() or 0
    delivered = db.query(func.count(Nudge.id)).filter(Nudge.created_at>=since, Nudge.delivered_at.isnot(None)).scalar() or 0
    read = db.query(func.count(Nudge.id)).filter(Nudge.created_at>=since, Nudge.read_at.isnot(None)).scalar() or 0
    clicked = db.query(func.count(Nudge.id)).filter(Nudge.created_at>=since, Nudge.clicked_at.isnot(None)).scalar() or 0
    by_type = {t:c for t,c in db.query(Nudge.nudge_type, func.count(Nudge.id)).filter(Nudge.created_at>=since).group_by(Nudge.nudge_type).all()}
    ts = max(total, 1); ds = max(delivered, 1)
    return {"total": total, "delivery_rate": round(delivered/ts,3), "open_rate": round(read/ds,3),
            "click_rate": round(clicked/ds,3), "by_type": by_type}

@ma.post("/admin/send-nudge")
def manual_nudge(req: ManualNudgeRequest, _=Depends(verify_api), db: Session=Depends(get_db)):
    eng = NudgeEngine(db)
    ids = [eng._create(uid, "student", req.nudge_type, req.title, req.body, "info", req.priority,
           req.cta_text, req.cta_url) for uid in req.user_ids]
    return {"sent": len([i for i in ids if i]), "ids": [i.id for i in ids if i]}

@ma.get("/admin/overview")
def overview(_=Depends(verify_api), db: Session=Depends(get_db)):
    now = datetime.utcnow(); today = now.replace(hour=0,minute=0,second=0)
    return {
        "nudges_today": db.query(func.count(Nudge.id)).filter(Nudge.created_at>=today).scalar() or 0,
        "pending": db.query(func.count(Nudge.id)).filter(Nudge.status=="pending").scalar() or 0,
        "at_risk_students": db.query(func.count(AttendanceTracker.id)).filter(AttendanceTracker.consecutive_misses>=3).scalar() or 0,
        "unviewed_assignments": db.query(func.count(AssignmentTracker.id)).filter(
            AssignmentTracker.first_viewed_at.is_(None), AssignmentTracker.submission_status!="submitted", AssignmentTracker.deadline>now).scalar() or 0,
        "unwatched_recordings": db.query(func.count(RecordingTracker.id)).filter(
            RecordingTracker.completed==False, RecordingTracker.expected_by<now).scalar() or 0,
        "total_students_tracked": db.query(func.count(func.distinct(AttendanceTracker.user_id))).scalar() or 0,
        "ai_enabled": settings.enable_dropout_prediction,
    }

# ============ AI ADMIN (train model) ============
@ma.post("/admin/train-dropout")
def train_dropout(_=Depends(verify_api), db: Session=Depends(get_db)):
    """Train dropout model from labeled data. Call after 6 months."""
    import pandas as pd, xgboost as xgb, os
    from sklearn.model_selection import train_test_split
    features = db.query(StudentFeatures).filter(StudentFeatures.dropped_out.isnot(None)).all()
    if len(features) < settings.min_training_records:
        return {"error": f"Need {settings.min_training_records}+ labeled records, have {len(features)}"}
    df = pd.DataFrame([{
        "login_frequency": f.login_frequency or 0,
        "avg_session_minutes": f.avg_session_minutes or 0,
        "score_trend": f.score_trend or 0,
        "consecutive_misses": f.consecutive_misses or 0,
        "assignment_completion_rate": f.assignment_completion_rate or 0,
        "recording_completion_rate": f.recording_completion_rate or 0,
        "days_since_last_login": f.days_since_last_login or 0,
        "total_nudges_received": f.total_nudges_received or 0,
        "nudge_response_rate": f.nudge_response_rate or 0,
        "dropped_out": f.dropped_out
    } for f in features])
    X = df.drop("dropped_out", axis=1); y = df["dropped_out"].astype(int)
    X_train, X_test, y_train, y_test = train_test_split(X, y, test_size=0.2, random_state=42)
    model = xgb.XGBClassifier(n_estimators=100, max_depth=5, eval_metric="logloss")
    model.fit(X_train, y_train)
    accuracy = model.score(X_test, y_test)
    os.makedirs(os.path.dirname(settings.dropout_model_path), exist_ok=True)
    model.save_model(settings.dropout_model_path)
    return {"ok": True, "records": len(features), "accuracy": round(accuracy, 4),
            "model_path": settings.dropout_model_path}