"""Recorded-lecture watch tracking and catch-up reminders."""
import logging
from datetime import datetime, timedelta
from typing import Dict, List, Optional

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.models import Nudge, RecordingTracker
from app.services.copy import render
from app.services.messages import MENTOR, RECORDING
from app.services.nudges import NudgeService

log = logging.getLogger("services.recordings")

#: Watch percentage at which a recording counts as complete.
COMPLETION_THRESHOLD = 80

#: Default grace period when the LMS does not supply `expected_by`.
DEFAULT_WATCH_WINDOW = timedelta(days=7)

#: Reminder budget per recording, and minimum spacing between reminders.
MAX_REMINDERS = 3
REMINDER_SPACING = timedelta(hours=24)

#: Stop chasing a recording once it is this far past its expected date.
CHASE_CUTOFF = timedelta(days=14)

#: Unwatched recordings a student must accumulate before the mentor is told.
MENTOR_ALERT_THRESHOLD = 3

#: Days overdue past which the copy switches from reminder to overdue framing.
OVERDUE_AFTER_DAYS = 3


def _parse_ts(value, fallback: datetime) -> datetime:
    """Parse an ISO timestamp, falling back when absent or malformed."""
    if isinstance(value, datetime):
        return value
    if isinstance(value, str) and value:
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            log.warning("Unparseable timestamp %r, using fallback", value)
    return fallback


def reminder_key(watch_percent: int, days_overdue: int) -> str:
    """Pick the message template for an unwatched recording.

    Pure function — unit-testable without a database.
    """
    if watch_percent > 0:
        return "partial"
    return "not_watched" if days_overdue <= OVERDUE_AFTER_DAYS else "overdue"


class RecordingService:
    """Tracks who watched recordings and chases those who did not."""

    def __init__(self, db: Session):
        self.db = db
        self.nudges = NudgeService(db)

    def register(
        self,
        lecture_id: str,
        course_id: str,
        batch_id: str,
        title: str,
        recording_url: str,
        uploaded_at: str,
        expected_by: str,
        student_ids: List[str],
        mentor_id: str = "",
    ) -> int:
        """Create a tracker row per student for a newly uploaded recording.

        Returns:
            Number of tracker rows created.
        """
        uploaded = _parse_ts(uploaded_at, datetime.utcnow())
        expected = _parse_ts(expected_by, uploaded + DEFAULT_WATCH_WINDOW)
        existing = self._existing_user_ids(lecture_id, student_ids)

        created = 0
        for student_id in student_ids:
            if student_id in existing:
                continue
            self.db.add(RecordingTracker(
                user_id=student_id, lecture_id=lecture_id, course_id=course_id,
                batch_id=batch_id, lecture_title=title, recording_url=recording_url,
                uploaded_at=uploaded, expected_by=expected, mentor_id=mentor_id,
                watch_percent=0, completed=False, reminder_count=0,
            ))
            created += 1
        self.db.commit()
        return created

    def _existing_user_ids(self, lecture_id: str, student_ids: List[str]) -> set:
        """User ids already tracked for this lecture (one query, not N)."""
        rows = self.db.query(RecordingTracker.user_id).filter(
            RecordingTracker.lecture_id == lecture_id,
            RecordingTracker.user_id.in_(student_ids),
        ).all()
        return {row[0] for row in rows}

    def update_progress(self, user_id: str, lecture_id: str, watch_percent: int) -> None:
        """Record watch progress, keeping the furthest point reached."""
        tracker = self.db.query(RecordingTracker).filter(
            RecordingTracker.user_id == user_id,
            RecordingTracker.lecture_id == lecture_id,
        ).first()
        if not tracker:
            log.info("Watch progress for untracked recording %s / %s", user_id, lecture_id)
            return

        now = datetime.utcnow()
        tracker.watch_percent = max(tracker.watch_percent or 0, watch_percent)
        if not tracker.first_watched_at:
            tracker.first_watched_at = now
        tracker.last_watched_at = now
        tracker.completed = tracker.watch_percent >= COMPLETION_THRESHOLD
        self.db.commit()

    def check_unwatched(self) -> List[Nudge]:
        """Cron job: chase overdue recordings and alert mentors on pile-ups.

        Returns:
            The student nudges actually created this run.
        """
        now = datetime.utcnow()
        overdue = self.db.query(RecordingTracker).filter(
            RecordingTracker.completed.is_(False),
            RecordingTracker.expected_by < now,
            RecordingTracker.expected_by > now - CHASE_CUTOFF,
            RecordingTracker.reminder_count < MAX_REMINDERS,
        ).all()

        sent = [n for n in (self._remind(t, now) for t in overdue) if n]
        self._alert_mentors(now)
        self.db.commit()
        return sent

    def _remind(self, tracker: RecordingTracker, now: datetime) -> Optional[Nudge]:
        """Send one catch-up reminder if this tracker is due for it."""
        if tracker.last_reminded_at and now - tracker.last_reminded_at < REMINDER_SPACING:
            return None

        days_overdue = (now - tracker.expected_by).days
        watched = tracker.watch_percent or 0
        key = reminder_key(watched, days_overdue)
        context = {
            "topic": tracker.lecture_title, "pct": watched,
            "days": days_overdue if watched else (now - tracker.uploaded_at).days,
        }

        message = render(RECORDING, key, context, nudge_type="recording_unwatched")
        nudge = self.nudges.create(
            user_id=tracker.user_id, role="student", nudge_type="recording_unwatched",
            title=message["title"], body=message["body"],
            template_id=message["template_id"],
            severity=message["severity"], priority="medium",
            cta_text=message["cta"], cta_url=f"/recordings/{tracker.lecture_id}",
        )
        if nudge:
            tracker.reminder_count = (tracker.reminder_count or 0) + 1
            tracker.last_reminded_at = now
        return nudge

    def _alert_mentors(self, now: datetime) -> None:
        """Tell each batch's mentor how many students are falling behind.

        Alerts go to the real mentor recorded when the recording was
        registered. If the LMS did not supply one, the alert is skipped and
        logged — writing to a synthesised ``mentor_{batch}`` id only creates
        nudges nobody will ever read.
        """
        behind = self.db.query(
            RecordingTracker.user_id, RecordingTracker.batch_id,
        ).filter(
            RecordingTracker.completed.is_(False),
            RecordingTracker.expected_by < now,
        ).group_by(
            RecordingTracker.user_id, RecordingTracker.batch_id,
        ).having(func.count(RecordingTracker.id) >= MENTOR_ALERT_THRESHOLD).all()

        students_per_batch: Dict[str, int] = {}
        for _user_id, batch_id in behind:
            students_per_batch[batch_id] = students_per_batch.get(batch_id, 0) + 1

        for batch_id, count in students_per_batch.items():
            mentor_id = self._mentor_for_batch(batch_id)
            if not mentor_id:
                log.warning(
                    "%d students behind in batch %s but no mentor_id is known — "
                    "alert skipped. Pass mentor_id on the recording-uploaded webhook.",
                    count, batch_id,
                )
                continue
            message = render(MENTOR, "recordings", {"count": count, "batch": batch_id},
                             nudge_type="mentor_alert", escalation=count)
            self.nudges.create(
                user_id=mentor_id, role="mentor", nudge_type="mentor_alert",
                title=message["title"], body=message["body"],
                template_id=message["template_id"],
                severity="warning", priority="medium", cta_text=message["cta"],
                meta={"batch_id": batch_id, "type": "recordings_behind"},
            )

    def _mentor_for_batch(self, batch_id: str) -> str:
        """Most recently recorded mentor for a batch, or "" if unknown."""
        row = self.db.query(RecordingTracker.mentor_id).filter(
            RecordingTracker.batch_id == batch_id,
            RecordingTracker.mentor_id != "",
        ).order_by(RecordingTracker.uploaded_at.desc()).first()
        return row[0] if row else ""

    def report(self, course_id: str = "", batch_id: str = "") -> List[Dict]:
        """Watch-status rows for the dashboard, optionally filtered."""
        query = self.db.query(RecordingTracker)
        if course_id:
            query = query.filter(RecordingTracker.course_id == course_id)
        if batch_id:
            query = query.filter(RecordingTracker.batch_id == batch_id)
        now = datetime.utcnow()
        return [self._report_row(t, now) for t in query.all()]

    @staticmethod
    def _report_row(tracker: RecordingTracker, now: datetime) -> Dict:
        """Shape one tracker into a dashboard row."""
        return {
            "user_id": tracker.user_id, "lecture_id": tracker.lecture_id,
            "title": tracker.lecture_title, "watch_pct": tracker.watch_percent or 0,
            "completed": bool(tracker.completed),
            "uploaded_at": str(tracker.uploaded_at),
            "expected_by": str(tracker.expected_by),
            "overdue": bool(
                not tracker.completed and tracker.expected_by and tracker.expected_by < now
            ),
            "days_since_upload": (now - tracker.uploaded_at).days,
        }
