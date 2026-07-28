"""LMS -> Agent event webhooks.

Handlers stay thin: authenticate, delegate to a service, shape the response.
No business logic lives here.
"""
import logging
from datetime import datetime

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import StudentFeatures
from app.routes.dependencies import error_response, verify_webhook
from app.schemas import (AssignmentSubmitEvent, AssignmentUploadEvent,
                         AssignmentViewEvent, LectureAttendanceEvent,
                         LoginEvent, QuizScoreEvent, RecordingUploadEvent,
                         RecordingWatchEvent)
from app.services.assignments import AssignmentService
from app.services.attendance import AttendanceService
from app.services.recordings import RecordingService
from app.services.topics import TopicService

log = logging.getLogger("routes.webhooks")
router = APIRouter(prefix="/webhook", tags=["Webhooks"], dependencies=[Depends(verify_webhook)])

#: Weight of one login when updating the rolling login frequency.
LOGIN_FREQUENCY_STEP = 1.0 / 7.0

#: Smoothing factor for the session-length moving average.
SESSION_EWMA_ALPHA = 0.5


@router.post("/lecture-attendance")
def lecture_attendance(event: LectureAttendanceEvent, db: Session = Depends(get_db)):
    """Record one attendance event. Idempotent on (user_id, lecture_id)."""
    try:
        nudge = AttendanceService(db).process(
            user_id=event.user_id, course_id=event.course_id,
            batch_id=event.batch_id, attended=event.attended,
            lecture_title=event.lecture_title, mentor_id=event.mentor_id,
            student_name=event.student_name, lecture_id=event.lecture_id,
        )
        return {"ok": True, "nudge_id": nudge.id if nudge else None}
    except Exception as exc:  # noqa: BLE001
        log.error("Attendance webhook failed: %s", exc)
        db.rollback()
        return error_response("attendance_failed", "Could not record attendance")


@router.post("/recording-uploaded")
def recording_uploaded(event: RecordingUploadEvent, db: Session = Depends(get_db)):
    """Register a new recording against every student in the batch."""
    try:
        tracked = RecordingService(db).register(
            lecture_id=event.lecture_id, course_id=event.course_id,
            batch_id=event.batch_id, title=event.lecture_title,
            recording_url=event.recording_url, uploaded_at=event.uploaded_at,
            expected_by=event.expected_by, student_ids=event.student_ids,
            mentor_id=event.mentor_id,
        )
        return {"ok": True, "tracked": tracked}
    except Exception as exc:  # noqa: BLE001
        log.error("Recording upload webhook failed: %s", exc)
        db.rollback()
        return error_response("recording_register_failed", "Could not register recording")


@router.post("/recording-watched")
def recording_watched(event: RecordingWatchEvent, db: Session = Depends(get_db)):
    """Update how far a student has watched a recording."""
    try:
        RecordingService(db).update_progress(
            event.user_id, event.lecture_id, event.watch_percent
        )
        return {"ok": True}
    except Exception as exc:  # noqa: BLE001
        log.error("Recording watch webhook failed: %s", exc)
        db.rollback()
        return error_response("watch_update_failed", "Could not update watch progress")


@router.post("/assignment-uploaded")
def assignment_uploaded(event: AssignmentUploadEvent, db: Session = Depends(get_db)):
    """Register new coursework against every student it is assigned to."""
    try:
        tracked = AssignmentService(db).register(
            assignment_id=event.assignment_id, course_id=event.course_id,
            title=event.title, deadline=event.deadline,
            student_ids=event.student_ids, assignment_type=event.assignment_type,
            closes_after_deadline=event.closes_after_deadline,
        )
        return {"ok": True, "tracked": tracked}
    except Exception as exc:  # noqa: BLE001
        log.error("Assignment upload webhook failed: %s", exc)
        db.rollback()
        return error_response("assignment_register_failed", "Could not register assignment")


@router.post("/assignment-viewed")
def assignment_viewed(event: AssignmentViewEvent, db: Session = Depends(get_db)):
    """Record the first time a student opened a piece of coursework."""
    try:
        AssignmentService(db).mark_viewed(event.assignment_id, event.user_id)
        return {"ok": True}
    except Exception as exc:  # noqa: BLE001
        log.error("Assignment view webhook failed: %s", exc)
        db.rollback()
        return error_response("assignment_view_failed", "Could not record view")


@router.post("/assignment-submitted")
def assignment_submitted(event: AssignmentSubmitEvent, db: Session = Depends(get_db)):
    """Record a submission and stop further deadline reminders."""
    try:
        AssignmentService(db).mark_submitted(event.assignment_id, event.user_id)
        return {"ok": True}
    except Exception as exc:  # noqa: BLE001
        log.error("Assignment submit webhook failed: %s", exc)
        db.rollback()
        return error_response("assignment_submit_failed", "Could not record submission")


@router.post("/quiz-scored")
def quiz_scored(event: QuizScoreEvent, db: Session = Depends(get_db)):
    """Record a quiz score and coach on weak topics."""
    try:
        nudge = TopicService(db).process_quiz(
            user_id=event.user_id, course_id=event.course_id,
            topic_name=event.topic_name, score=event.score,
            batch_average=event.batch_average, student_name=event.student_name,
            mentor_id=event.mentor_id,
        )
        return {"ok": True, "nudge_id": nudge.id if nudge else None}
    except Exception as exc:  # noqa: BLE001
        log.error("Quiz webhook failed: %s", exc)
        db.rollback()
        return error_response("quiz_failed", "Could not record quiz score")


@router.post("/login")
def login(event: LoginEvent, db: Session = Depends(get_db)):
    """Record a login for the dropout feature set."""
    try:
        features = db.query(StudentFeatures).filter(
            StudentFeatures.user_id == event.user_id,
            StudentFeatures.course_id == event.course_id,
        ).first()
        if not features:
            features = StudentFeatures(user_id=event.user_id, course_id=event.course_id)
            db.add(features)

        features.login_frequency = (features.login_frequency or 0) + LOGIN_FREQUENCY_STEP
        features.avg_session_minutes = (
            (features.avg_session_minutes or 0) * (1 - SESSION_EWMA_ALPHA)
            + event.session_minutes * SESSION_EWMA_ALPHA
        )
        features.days_since_last_login = 0
        features.last_login_at = datetime.utcnow()
        db.commit()
        return {"ok": True}
    except Exception as exc:  # noqa: BLE001
        log.error("Login webhook failed: %s", exc)
        db.rollback()
        return error_response("login_failed", "Could not record login")
