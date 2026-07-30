"""Assignment and case-study tracking with deadline reminders."""
import logging
from datetime import datetime, timedelta
from typing import Dict, List, Optional

from sqlalchemy.orm import Session

from app.models import AssignmentTracker, Nudge
from app.services.messages import ASSIGNMENT, get_msg
from app.services.nudges import NudgeService

log = logging.getLogger("services.assignments")

#: Deadline reminder stages, ordered most-urgent first. The first stage whose
#: `max_hours_left` still covers the remaining time wins.
DEADLINE_STAGES = [
    {"max_hours_left": 6, "template_key": "6_hours", "priority": "critical"},
    {"max_hours_left": 24, "template_key": "1_day", "priority": "critical"},
    {"max_hours_left": 72, "template_key": "3_days", "priority": "high"},
]

#: Fallback stage when the deadline is distant but the student never opened it.
UNVIEWED_STAGE = {"template_key": "not_viewed_48h", "priority": "medium"}
UNVIEWED_AFTER = timedelta(hours=48)

#: Reminder budget per assignment, and minimum spacing between reminders.
MAX_REMINDERS = 5
REMINDER_SPACING = timedelta(hours=12)

#: Default window used when an assignment has no explicit type.
DEFAULT_TYPE = "assignment"


def stage_for(hours_left: float, viewed: bool, hours_since_upload: float) -> Optional[Dict]:
    """Pick the reminder stage for an unsubmitted assignment.

    Pure function — unit-testable without a database.

    Args:
        hours_left: Hours until the deadline.
        viewed: Whether the student has ever opened it.
        hours_since_upload: Hours since it was published.

    Returns:
        The matching stage dict, or None if no reminder is due.
    """
    for stage in DEADLINE_STAGES:
        if hours_left <= stage["max_hours_left"]:
            return stage
    if not viewed and hours_since_upload > UNVIEWED_AFTER.total_seconds() / 3600:
        return UNVIEWED_STAGE
    return None


class AssignmentService:
    """Registers assignments and chases unsubmitted ones as deadlines close."""

    def __init__(self, db: Session):
        self.db = db
        self.nudges = NudgeService(db)

    def register(
        self,
        assignment_id: str,
        course_id: str,
        title: str,
        deadline: str,
        student_ids: List[str],
        assignment_type: str = DEFAULT_TYPE,
        closes_after_deadline: bool = True,
    ) -> int:
        """Create a tracker row per student for a newly published assignment.

        Returns:
            Number of tracker rows created.
        """
        due = datetime.fromisoformat(deadline) if isinstance(deadline, str) else deadline
        existing = self._existing_user_ids(assignment_id, student_ids)
        created = 0
        for student_id in student_ids:
            if student_id in existing:
                continue
            self.db.add(AssignmentTracker(
                assignment_id=assignment_id, user_id=student_id,
                course_id=course_id, title=title, assignment_type=assignment_type,
                uploaded_at=datetime.utcnow(), deadline=due,
                closes_after_deadline=closes_after_deadline,
                submission_status="not_started", reminder_count=0,
            ))
            created += 1
        self.db.commit()
        return created

    def _existing_user_ids(self, assignment_id: str, student_ids: List[str]) -> set:
        """User ids already tracked for this assignment.

        One query with a set membership check, rather than a per-student query
        inside the loop (Standards §2).
        """
        rows = self.db.query(AssignmentTracker.user_id).filter(
            AssignmentTracker.assignment_id == assignment_id,
            AssignmentTracker.user_id.in_(student_ids),
        ).all()
        return {row[0] for row in rows}

    def mark_viewed(self, assignment_id: str, user_id: str) -> None:
        """Record the first time a student opened the assignment."""
        tracker = self._tracker(assignment_id, user_id)
        if tracker and not tracker.first_viewed_at:
            tracker.first_viewed_at = datetime.utcnow()
            self.db.commit()

    def mark_submitted(self, assignment_id: str, user_id: str) -> None:
        """Record a submission and stop further reminders."""
        tracker = self._tracker(assignment_id, user_id)
        if tracker:
            tracker.submitted_at = datetime.utcnow()
            tracker.submission_status = "submitted"
            self.db.commit()

    def _tracker(self, assignment_id: str, user_id: str) -> Optional[AssignmentTracker]:
        """One student's tracker row for one assignment."""
        return self.db.query(AssignmentTracker).filter(
            AssignmentTracker.assignment_id == assignment_id,
            AssignmentTracker.user_id == user_id,
        ).first()

    def check_deadlines(self) -> List[Nudge]:
        """Cron job: remind students whose deadlines are approaching.

        Returns:
            The nudges actually created this run.
        """
        now = datetime.utcnow()
        pending = self.db.query(AssignmentTracker).filter(
            AssignmentTracker.submission_status != "submitted",
            AssignmentTracker.deadline > now,
        ).all()

        sent = []
        for tracker in pending:
            nudge = self._remind(tracker, now)
            if nudge:
                sent.append(nudge)
        self.db.commit()
        return sent

    def _remind(self, tracker: AssignmentTracker, now: datetime) -> Optional[Nudge]:
        """Send one deadline reminder if this tracker is due for it."""
        if (tracker.reminder_count or 0) >= MAX_REMINDERS:
            return None
        if tracker.last_reminded_at and now - tracker.last_reminded_at < REMINDER_SPACING:
            return None

        hours_left = (tracker.deadline - now).total_seconds() / 3600
        hours_since_upload = (now - tracker.uploaded_at).total_seconds() / 3600
        stage = stage_for(hours_left, bool(tracker.first_viewed_at), hours_since_upload)
        if not stage:
            return None

        message = get_msg(ASSIGNMENT, stage["template_key"], {
            "title": tracker.title, "type": tracker.assignment_type,
            "days": (now - tracker.uploaded_at).days, "hours": round(hours_left),
        })
        nudge = self.nudges.create(
            user_id=tracker.user_id, role="student", nudge_type="assignment_deadline",
            title=message["title"], body=message["body"],
            severity=message["severity"], priority=stage["priority"],
            cta_text=message["cta"], cta_url=f"/assignments/{tracker.assignment_id}",
            # `type` is what lets the client tell a capstone from a case study
            # from an industry session — they all arrive as assignment_deadline,
            # and without it every one of them renders under "Assignments".
            meta={"assignment_id": tracker.assignment_id, "title": tracker.title,
                  "type": tracker.assignment_type,
                  "hours": round(hours_left), "hours_left": round(hours_left),
                  "url": f"/assignments/{tracker.assignment_id}"},
            # Routing level = the stage's hour threshold, so 6h can reach
            # WhatsApp while 72h stays on the dashboard.
            escalation=stage.get("max_hours_left", 0),
        )
        if nudge:
            tracker.reminder_count = (tracker.reminder_count or 0) + 1
            tracker.last_reminded_at = now
        return nudge

    def for_student(self, user_id: str) -> List[Dict]:
        """All tracked assignments for one student, soonest deadline first."""
        now = datetime.utcnow()
        trackers = self.db.query(AssignmentTracker).filter(
            AssignmentTracker.user_id == user_id,
        ).order_by(AssignmentTracker.deadline).all()
        return [self._student_row(t, now) for t in trackers]

    @staticmethod
    def _student_row(tracker: AssignmentTracker, now: datetime) -> Dict:
        """Shape one tracker for the student assignments list."""
        hours_left = (tracker.deadline - now).total_seconds() / 3600
        status = tracker.submission_status or "not_started"
        return {
            "id": tracker.assignment_id, "title": tracker.title,
            "type": tracker.assignment_type, "deadline": str(tracker.deadline),
            "viewed": tracker.first_viewed_at is not None, "status": status,
            "hours_left": max(0, round(hours_left, 1)),
            "urgency": "critical" if hours_left < 24 and status != "submitted" else "normal",
        }
