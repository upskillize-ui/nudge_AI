"""Live-lecture attendance tracking and the consecutive-miss escalation."""
import logging
from datetime import datetime
from typing import Dict, List, Optional

from sqlalchemy.orm import Session

from app.models import AttendanceTracker, Nudge
from app.services.messages import MENTOR, MISS, get_msg
from app.services.nudges import NudgeService
from app.services.streaks import StreakService

log = logging.getLogger("services.attendance")

#: Escalation ladder, ordered by descending threshold. The first rung whose
#: `min_misses` is met wins. Adding a rung means editing this table, not the
#: control flow below (Standards §1 "config over code", §3 registry pattern).
ESCALATION_LADDER = [
    {"min_misses": 5, "template_key": 5, "priority": "critical", "alert_mentor": True},
    {"min_misses": 3, "template_key": 3, "priority": "critical", "alert_mentor": True},
    {"min_misses": 2, "template_key": 2, "priority": "high", "alert_mentor": False},
    {"min_misses": 1, "template_key": 1, "priority": "medium", "alert_mentor": False},
]

#: Escalation level stored on the tracker is capped at this value.
MAX_ESCALATION_LEVEL = 4

#: Counters that legacy rows may have as NULL.
_NULLABLE_COUNTERS = (
    "total_lectures", "attended_count", "consecutive_misses",
    "max_consecutive", "escalation_level",
)


def rung_for(consecutive_misses: int) -> Optional[Dict]:
    """Return the escalation rung for a miss count, or None below threshold.

    Pure function — unit-testable without a database.

    Args:
        consecutive_misses: Number of consecutive lectures missed.

    Returns:
        The matching ladder entry, or None if no rung applies.
    """
    for rung in ESCALATION_LADDER:
        if consecutive_misses >= rung["min_misses"]:
            return rung
    return None


class AttendanceService:
    """Records attendance and escalates on consecutive misses."""

    def __init__(self, db: Session):
        self.db = db
        self.nudges = NudgeService(db)
        self.streaks = StreakService(db)

    def process(
        self,
        user_id: str,
        course_id: str,
        batch_id: str,
        attended: bool,
        lecture_title: str = "",
        mentor_id: str = "",
        student_name: str = "",
        lecture_id: str = "",
    ) -> Optional[Nudge]:
        """Record one attendance event and nudge if the student is slipping.

        Idempotent on (user_id, lecture_id): a webhook retry carrying the same
        lecture_id returns early without double-counting.

        Returns:
            The student nudge if one was created, else None.
        """
        tracker = self._get_or_create_tracker(user_id, course_id, batch_id)

        if lecture_id and getattr(tracker, "last_lecture_id", "") == lecture_id:
            log.info("Attendance dedup: %s / %s already counted", user_id, lecture_id)
            return None

        self._normalise_counters(tracker)
        tracker.total_lectures += 1
        if lecture_id and hasattr(tracker, "last_lecture_id"):
            tracker.last_lecture_id = lecture_id

        if attended:
            self._record_present(tracker)
            # A miss resets the streak silently; a milestone celebrates.
            self.streaks.record(user_id, course_id, True, lecture_title)
            return None
        self.streaks.record(user_id, course_id, False, lecture_title)
        return self._record_absent(
            tracker, batch_id, lecture_title, mentor_id, student_name, course_id
        )

    def _get_or_create_tracker(
        self, user_id: str, course_id: str, batch_id: str
    ) -> AttendanceTracker:
        """Fetch this student's tracker for the course, creating it if absent."""
        tracker = self.db.query(AttendanceTracker).filter(
            AttendanceTracker.user_id == user_id,
            AttendanceTracker.course_id == course_id,
        ).first()
        if tracker:
            return tracker
        tracker = AttendanceTracker(
            user_id=user_id, course_id=course_id, batch_id=batch_id,
            total_lectures=0, attended_count=0, consecutive_misses=0,
            max_consecutive=0, escalation_level=0,
        )
        self.db.add(tracker)
        self.db.flush()
        return tracker

    @staticmethod
    def _normalise_counters(tracker: AttendanceTracker) -> None:
        """Coerce NULL counters on legacy rows to 0 so arithmetic is safe."""
        for field in _NULLABLE_COUNTERS:
            if getattr(tracker, field) is None:
                setattr(tracker, field, 0)

    def _record_present(self, tracker: AttendanceTracker) -> None:
        """Mark attendance and reset the escalation state."""
        tracker.attended_count += 1
        tracker.consecutive_misses = 0
        tracker.last_attended_at = datetime.utcnow()
        tracker.escalation_level = 0
        self.db.commit()

    def _record_absent(
        self,
        tracker: AttendanceTracker,
        batch_id: str,
        lecture_title: str,
        mentor_id: str,
        student_name: str,
        course_id: str,
    ) -> Optional[Nudge]:
        """Increment the miss counter and send the matching ladder nudge."""
        tracker.consecutive_misses += 1
        tracker.last_missed_at = datetime.utcnow()
        tracker.max_consecutive = max(tracker.max_consecutive, tracker.consecutive_misses)
        self.db.commit()

        misses = tracker.consecutive_misses
        rung = rung_for(misses)
        if not rung:
            return None

        name = student_name or tracker.user_id
        attended_pct = round(
            (tracker.attended_count / max(tracker.total_lectures, 1)) * 100
        )
        context = {
            "name": name, "topic": lecture_title,
            "mentor": mentor_id or "your mentor",
            "pct": attended_pct, "misses": misses,
        }

        tracker.escalation_level = min(rung["template_key"], MAX_ESCALATION_LEVEL)
        tracker.last_nudge_at = datetime.utcnow()
        self.db.commit()

        message = get_msg(MISS, rung["template_key"], context)
        nudge = self.nudges.create(
            user_id=tracker.user_id, role="student", nudge_type="consecutive_miss",
            title=message["title"], body=message["body"],
            severity=message["severity"], priority=rung["priority"],
            cta_text=message["cta"], cta_url=f"/courses/{course_id}/recordings",
            meta={"misses": misses, "course_id": course_id, "pct": attended_pct,
                  "course": lecture_title},
            escalation=rung["template_key"],
        )

        if rung["alert_mentor"] and mentor_id:
            self._alert_mentor(mentor_id, tracker, name, batch_id, misses)
        return nudge

    def _alert_mentor(
        self,
        mentor_id: str,
        tracker: AttendanceTracker,
        student_name: str,
        batch_id: str,
        misses: int,
    ) -> None:
        """Notify the mentor that a student has crossed the alert threshold."""
        message = get_msg(MENTOR, "miss", {
            "student": student_name, "batch": batch_id, "misses": misses,
            "last_active": str(tracker.last_attended_at or "unknown"),
        })
        self.nudges.create(
            user_id=mentor_id, role="mentor", nudge_type="mentor_alert",
            title=message["title"], body=message["body"],
            severity="critical", priority="critical", cta_text=message["cta"],
            meta={"student_id": tracker.user_id, "misses": misses,
                  "type": "consecutive_miss"},
        )

    def report(self, course_id: str = "", batch_id: str = "") -> List[Dict]:
        """Attendance summary rows for the dashboard, optionally filtered."""
        query = self.db.query(AttendanceTracker)
        if course_id:
            query = query.filter(AttendanceTracker.course_id == course_id)
        if batch_id:
            query = query.filter(AttendanceTracker.batch_id == batch_id)
        return [self._report_row(t) for t in query.all()]

    @staticmethod
    def _report_row(tracker: AttendanceTracker) -> Dict:
        """Shape one tracker into a dashboard row."""
        total = tracker.total_lectures or 0
        attended = tracker.attended_count or 0
        misses = tracker.consecutive_misses or 0
        return {
            "user_id": tracker.user_id, "course_id": tracker.course_id,
            "batch_id": tracker.batch_id, "total": total, "attended": attended,
            "pct": round((attended / max(total, 1)) * 100),
            "consecutive_misses": misses,
            "max_consecutive": tracker.max_consecutive or 0,
            "last_attended": str(tracker.last_attended_at) if tracker.last_attended_at else None,
            "status": "critical" if misses >= 3 else ("warning" if misses >= 2 else "ok"),
        }
