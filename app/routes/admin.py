"""Mentor and admin endpoints: reports, analytics, manual send, ML training."""
import logging
import os
import re
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.config import get_settings
from app.database import get_db
from app.models import Nudge, StudentFeatures
from app.routes.dependencies import verify_api
from app.schemas import ManualNudgeRequest
from app.services.assignments import AssignmentService
from app.services.dropout import FEATURE_COLUMNS, DropoutService
from app.services.nudges import NudgeService
from app.services.recordings import RecordingService
from app.services.reports import ReportService

log = logging.getLogger("routes.admin")
router = APIRouter(tags=["Mentor & Admin"], dependencies=[Depends(verify_api)])
settings = get_settings()

#: Accepted shape for the analytics period parameter, e.g. "7d", "30d".
PERIOD_PATTERN = re.compile(r"^(\d{1,4})d$")

#: Longest analytics window we will serve.
MAX_PERIOD_DAYS = 365

#: Roles a manual nudge may target.
MANUAL_ROLES = {"student", "mentor"}

#: Train/test split and model shape for the dropout classifier.
TEST_SPLIT = 0.2
RANDOM_SEED = 42
N_ESTIMATORS = 100
MAX_DEPTH = 5


@router.get("/mentor/critical-students")
def critical_students(batch_id: str = Query(""), db: Session = Depends(get_db)):
    """Students needing attention, most severe first."""
    return {"students": ReportService(db).critical_students(batch_id)}


@router.get("/student/improvements")
def student_improvements(user_id: str = Query(...), db: Session = Depends(get_db)):
    """Everything the student Insights tab renders."""
    return ReportService(db).student_improvements(user_id)


@router.get("/student/assignments")
def student_assignments(user_id: str = Query(...), db: Session = Depends(get_db)):
    """One student's coursework, soonest deadline first."""
    return {"assignments": AssignmentService(db).for_student(user_id)}


@router.get("/reports/attendance")
def attendance_report(
    course_id: str = Query(""),
    batch_id: str = Query(""),
    db: Session = Depends(get_db),
):
    """Attendance summary rows."""
    return {"report": ReportService(db).attendance(course_id, batch_id)}


@router.get("/reports/recordings")
def recording_report(
    course_id: str = Query(""),
    batch_id: str = Query(""),
    db: Session = Depends(get_db),
):
    """Recording watch-status rows."""
    return {"report": RecordingService(db).report(course_id, batch_id)}


@router.get("/admin/overview")
def overview(db: Session = Depends(get_db)):
    """Headline counters for the dashboard."""
    return {**ReportService(db).overview(), "ai_enabled": settings.enable_dropout_prediction}


@router.get("/admin/analytics")
def analytics(period: str = Query("30d"), db: Session = Depends(get_db)):
    """Delivery, open and click rates over a trailing window."""
    match = PERIOD_PATTERN.match(period)
    if not match:
        raise HTTPException(400, "period must look like '7d', '30d' or '90d'")

    days = min(int(match.group(1)), MAX_PERIOD_DAYS)
    since = datetime.utcnow() - timedelta(days=days)

    def count(*filters) -> int:
        return db.query(func.count(Nudge.id)).filter(
            Nudge.created_at >= since, *filters
        ).scalar() or 0

    total = count()
    delivered = count(Nudge.delivered_at.isnot(None))
    read = count(Nudge.read_at.isnot(None))
    clicked = count(Nudge.clicked_at.isnot(None))
    by_type = dict(
        db.query(Nudge.nudge_type, func.count(Nudge.id))
        .filter(Nudge.created_at >= since)
        .group_by(Nudge.nudge_type).all()
    )

    return {
        "total": total,
        "delivery_rate": round(delivered / max(total, 1), 3),
        "open_rate": round(read / max(delivered, 1), 3),
        "click_rate": round(clicked / max(delivered, 1), 3),
        "by_type": by_type,
    }


@router.get("/admin/recent-nudges")
def recent_nudges(limit: int = Query(20, le=100), db: Session = Depends(get_db)):
    """Latest nudges across all users, for the dashboard activity table."""
    nudges = db.query(Nudge).order_by(Nudge.created_at.desc()).limit(limit).all()
    return {"nudges": [{
        "id": n.id, "user_id": n.user_id, "type": n.nudge_type,
        "priority": n.priority, "title": n.title, "body": n.body,
        "severity": n.severity, "status": n.status,
        "cta_text": n.cta_text or "", "cta_url": n.cta_url or "",
        "created_at": str(n.created_at),
    } for n in nudges]}


@router.post("/admin/send-nudge")
def send_nudge(request: ManualNudgeRequest, db: Session = Depends(get_db)):
    """Send an ad-hoc nudge to a list of users."""
    role = getattr(request, "role", "student") or "student"
    if role not in MANUAL_ROLES:
        raise HTTPException(400, f"role must be one of {sorted(MANUAL_ROLES)}")

    service = NudgeService(db)
    created = [
        nudge.id for nudge in (
            service.create(
                user_id=user_id, role=role, nudge_type=request.nudge_type,
                title=request.title, body=request.body,
                severity=request.severity, priority=request.priority,
                cta_text=request.cta_text, cta_url=request.cta_url,
            )
            for user_id in request.user_ids
        ) if nudge
    ]
    return {"sent": len(created), "ids": created}


@router.post("/admin/aggregate-features")
def aggregate_features(db: Session = Depends(get_db)):
    """Run the nightly feature aggregation now (useful for backfill)."""
    return {"ok": True, "rows_updated": DropoutService(db).aggregate_features()}


@router.post("/admin/train-dropout")
def train_dropout(db: Session = Depends(get_db)):
    """Train the dropout classifier from labelled rows."""
    import pandas as pd
    import xgboost as xgb
    from sklearn.model_selection import train_test_split

    labelled = db.query(StudentFeatures).filter(
        StudentFeatures.dropped_out.isnot(None)
    ).all()
    if len(labelled) < settings.min_training_records:
        raise HTTPException(
            400,
            f"Need {settings.min_training_records}+ labelled records, "
            f"have {len(labelled)}",
        )

    frame = pd.DataFrame([
        {**DropoutService.feature_vector(row), "dropped_out": row.dropped_out}
        for row in labelled
    ])
    features = frame[FEATURE_COLUMNS]
    labels = frame["dropped_out"].astype(int)

    x_train, x_test, y_train, y_test = train_test_split(
        features, labels, test_size=TEST_SPLIT, random_state=RANDOM_SEED
    )
    model = xgb.XGBClassifier(
        n_estimators=N_ESTIMATORS, max_depth=MAX_DEPTH, eval_metric="logloss"
    )
    model.fit(x_train, y_train)
    accuracy = model.score(x_test, y_test)

    os.makedirs(os.path.dirname(settings.dropout_model_path), exist_ok=True)
    model.save_model(settings.dropout_model_path)

    return {
        "ok": True, "records": len(labelled),
        "accuracy": round(accuracy, 4),
        "model_path": settings.dropout_model_path,
    }
