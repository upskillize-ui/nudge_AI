"""Agent -> LMS nudge feed: what the notification bell reads."""
import logging
from datetime import datetime
from typing import Dict, List

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import Nudge, NudgeEvent
from app.routes.dependencies import verify_api
from app.schemas import StatusUpdate

log = logging.getLogger("routes.feed")
router = APIRouter(prefix="/nudges", tags=["Nudge Feed"], dependencies=[Depends(verify_api)])

#: Statuses that count as "not yet dealt with by the user".
UNREAD_STATUSES = ["pending", "delivered"]

#: Status transitions a client may request.
ALLOWED_STATUSES = {"read", "clicked", "dismissed"}

#: Timestamp column set by each status transition.
STATUS_TIMESTAMP = {
    "read": "read_at",
    "clicked": "clicked_at",
    "dismissed": "dismissed_at",
}


def _serialise(nudge: Nudge) -> Dict:
    """Shape one nudge for the client."""
    return {
        "id": nudge.id, "type": nudge.nudge_type, "priority": nudge.priority,
        "title": nudge.title, "body": nudge.body, "cta_text": nudge.cta_text,
        "cta_url": nudge.cta_url, "severity": nudge.severity,
        "status": nudge.status, "created_at": str(nudge.created_at),
        "meta": nudge.metadata_json,
    }


@router.get("/feed")
def get_feed(
    user_id: str = Query(...),
    role: str = Query("student"),
    status: str = Query("unread"),
    limit: int = Query(20, le=100),
    db: Session = Depends(get_db),
):
    """Return a user's nudges, marking pending ones as delivered."""
    query = db.query(Nudge).filter(Nudge.user_id == user_id, Nudge.user_role == role)
    if status == "unread":
        query = query.filter(Nudge.status.in_(UNREAD_STATUSES))
    query = query.filter(
        (Nudge.expires_at > datetime.utcnow()) | (Nudge.expires_at.is_(None))
    )

    nudges: List[Nudge] = query.order_by(Nudge.created_at.desc()).limit(limit).all()
    now = datetime.utcnow()
    for nudge in nudges:
        if nudge.status == "pending":
            nudge.status = "delivered"
            nudge.delivered_at = now
    db.commit()

    return {"nudges": [_serialise(n) for n in nudges], "total_unread": _unread(db, user_id, role)}


@router.get("/unread-count")
def unread_count(
    user_id: str = Query(...),
    role: str = Query("student"),
    db: Session = Depends(get_db),
):
    """Badge count, plus a breakdown by severity."""
    by_severity = dict(
        db.query(Nudge.severity, func.count(Nudge.id)).filter(
            Nudge.user_id == user_id,
            Nudge.user_role == role,
            Nudge.status.in_(UNREAD_STATUSES),
        ).group_by(Nudge.severity).all()
    )
    return {"total": _unread(db, user_id, role), "by_severity": by_severity}


def _unread(db: Session, user_id: str, role: str) -> int:
    """Count a user's undealt-with nudges."""
    return db.query(func.count(Nudge.id)).filter(
        Nudge.user_id == user_id,
        Nudge.user_role == role,
        Nudge.status.in_(UNREAD_STATUSES),
    ).scalar() or 0


@router.patch("/{nudge_id}/status")
def update_status(nudge_id: str, update: StatusUpdate, db: Session = Depends(get_db)):
    """Mark a nudge read, clicked or dismissed, and log the event."""
    if update.status not in ALLOWED_STATUSES:
        raise HTTPException(400, f"status must be one of {sorted(ALLOWED_STATUSES)}")

    nudge = db.query(Nudge).filter(Nudge.id == nudge_id).first()
    if not nudge:
        raise HTTPException(404, "nudge not found")

    now = datetime.utcnow()
    nudge.status = update.status
    setattr(nudge, STATUS_TIMESTAMP[update.status], now)
    if update.status == "clicked" and not nudge.read_at:
        nudge.read_at = now  # a click implies a read

    db.add(NudgeEvent(
        nudge_id=nudge_id, user_id=nudge.user_id,
        event_type=update.status, channel=nudge.channel,
    ))
    db.commit()
    return {"ok": True, "status": update.status}


@router.post("/batch-read")
def batch_read(user_id: str = Query(...), db: Session = Depends(get_db)):
    """Mark every outstanding nudge for a user as read."""
    updated = db.query(Nudge).filter(
        Nudge.user_id == user_id,
        Nudge.status.in_(UNREAD_STATUSES),
    ).update(
        {"status": "read", "read_at": datetime.utcnow()},
        synchronize_session="fetch",
    )
    db.commit()
    return {"updated": updated}
