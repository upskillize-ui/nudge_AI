"""Read-only aggregations for the dashboard and the student Insights tab.

No writes and no nudge creation happen here — this module only reads.
"""
import logging
from datetime import datetime, timedelta
from typing import Dict, List

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.models import (AssignmentTracker, AttendanceTracker, Nudge,
                        RecordingTracker)
from app.services.attendance import AttendanceService
from app.services.topics import TopicService

log = logging.getLogger("services.reports")

#: Consecutive misses at which a student appears on the at-risk list.
AT_RISK_MISSES = 3

#: Consecutive misses at which that risk is labelled critical rather than high.
CRITICAL_MISSES = 5

#: How far ahead an unopened assignment counts as urgent.
UNVIEWED_HORIZON = timedelta(days=3)

#: Sort order for the at-risk list.
RISK_ORDER = {"critical": 0, "high": 1, "medium": 2}

#: Thresholds that drive the student-facing tips.
TARGET_ATTENDANCE = 80
TIP_MISS_THRESHOLD = 2
TIP_ASSIGNMENT_THRESHOLD = 3
TIP_RECORDING_THRESHOLD = 3


class ReportService:
    """Builds dashboard and student-insight views."""

    def __init__(self, db: Session):
        self.db = db

    def overview(self) -> Dict:
        """Headline counters for the admin dashboard."""
        now = datetime.utcnow()
        today = now.replace(hour=0, minute=0, second=0, microsecond=0)
        return {
            "nudges_today": self._count(Nudge, Nudge.created_at >= today),
            "pending": self._count(Nudge, Nudge.status == "pending"),
            "at_risk_students": self._count(
                AttendanceTracker,
                AttendanceTracker.consecutive_misses >= AT_RISK_MISSES,
            ),
            "unviewed_assignments": self._count(
                AssignmentTracker,
                AssignmentTracker.first_viewed_at.is_(None),
                AssignmentTracker.submission_status != "submitted",
                AssignmentTracker.deadline > now,
            ),
            "unwatched_recordings": self._count(
                RecordingTracker,
                RecordingTracker.completed.is_(False),
                RecordingTracker.expected_by < now,
            ),
            "total_students_tracked": self.db.query(
                func.count(func.distinct(AttendanceTracker.user_id))
            ).scalar() or 0,
        }

    def _count(self, model, *filters) -> int:
        """Count rows of `model` matching `filters`."""
        return self.db.query(func.count(model.id)).filter(*filters).scalar() or 0

    def critical_students(self, batch_id: str = "") -> List[Dict]:
        """Students needing attention, most severe first."""
        results = self._miss_risks(batch_id)
        results.extend(self._assignment_risks())
        results.extend(self._recording_risks())
        results.sort(key=lambda r: RISK_ORDER.get(r["risk"], 3))
        return results

    def _miss_risks(self, batch_id: str) -> List[Dict]:
        """Students over the consecutive-miss threshold."""
        query = self.db.query(AttendanceTracker).filter(
            AttendanceTracker.consecutive_misses >= AT_RISK_MISSES,
        )
        if batch_id:
            query = query.filter(AttendanceTracker.batch_id == batch_id)
        return [{
            "user_id": t.user_id, "batch_id": t.batch_id,
            "type": "consecutive_miss",
            "risk": "critical" if (t.consecutive_misses or 0) >= CRITICAL_MISSES else "high",
            "misses": t.consecutive_misses or 0,
            "last_active": str(t.last_attended_at) if t.last_attended_at else None,
        } for t in query.all()]

    def _assignment_risks(self) -> List[Dict]:
        """Assignments closing soon that the student has never opened."""
        now = datetime.utcnow()
        trackers = self.db.query(AssignmentTracker).filter(
            AssignmentTracker.first_viewed_at.is_(None),
            AssignmentTracker.submission_status != "submitted",
            AssignmentTracker.deadline <= now + UNVIEWED_HORIZON,
            AssignmentTracker.deadline > now,
        ).all()
        return [{
            "user_id": t.user_id, "type": "assignment_unviewed", "risk": "high",
            "title": t.title,
            "hours_left": round((t.deadline - now).total_seconds() / 3600),
        } for t in trackers]

    def _recording_risks(self) -> List[Dict]:
        """Recordings past their expected date and still unwatched."""
        now = datetime.utcnow()
        trackers = self.db.query(RecordingTracker).filter(
            RecordingTracker.completed.is_(False),
            RecordingTracker.expected_by < now,
        ).all()
        return [{
            "user_id": t.user_id, "type": "recording_unwatched",
            "risk": "medium", "lecture": t.lecture_title,
        } for t in trackers]

    def student_improvements(self, user_id: str) -> Dict:
        """Everything the student Insights tab renders."""
        topics = TopicService(self.db).for_student(user_id)
        stats = self._student_stats(user_id)
        return {
            **topics,
            **stats,
            "tips": self._tips(stats, topics["weak"]),
        }

    def _student_stats(self, user_id: str) -> Dict:
        """Aggregate counters for one student across all their courses."""
        trackers = self.db.query(AttendanceTracker).filter(
            AttendanceTracker.user_id == user_id,
        ).all()
        total = sum(t.total_lectures or 0 for t in trackers)
        attended = sum(t.attended_count or 0 for t in trackers)
        now = datetime.utcnow()
        return {
            "attendance_pct": round((attended / total) * 100) if total else None,
            "consecutive_misses": max(
                (t.consecutive_misses or 0 for t in trackers), default=0
            ),
            "assignments_pending": self._count(
                AssignmentTracker,
                AssignmentTracker.user_id == user_id,
                AssignmentTracker.submission_status != "submitted",
                AssignmentTracker.deadline > now,
            ),
            "recordings_unwatched": self._count(
                RecordingTracker,
                RecordingTracker.user_id == user_id,
                RecordingTracker.completed.is_(False),
            ),
        }

    @staticmethod
    def _tips(stats: Dict, weak_topics: List[Dict]) -> List[str]:
        """Rule-derived coaching tips. Pure — no I/O."""
        tips = []
        attendance = stats["attendance_pct"]
        if attendance is not None and attendance < TARGET_ATTENDANCE:
            tips.append(
                f"Attendance is at {attendance}%. Aim for 85%+ to stay placement-eligible."
            )
        if stats["consecutive_misses"] >= TIP_MISS_THRESHOLD:
            tips.append(
                "You've missed multiple sessions in a row. "
                "Watch the recordings before the next live class."
            )
        pending = stats["assignments_pending"]
        if pending >= TIP_ASSIGNMENT_THRESHOLD:
            tips.append(f"{pending} assignments pending. Start with the one closing soonest.")
        elif pending >= 1:
            tips.append("Open your pending assignment now — even a draft submission counts.")
        if stats["recordings_unwatched"] >= TIP_RECORDING_THRESHOLD:
            tips.append("Recordings are stacking up. 30 minutes a day catches you up in a week.")
        if weak_topics:
            focus = ", ".join(t["topic"] for t in weak_topics[:2])
            tips.append(f"Focus area: revisit {focus} with practice questions.")
        return tips or ["You're on track — keep the momentum going."]

    def attendance(self, course_id: str = "", batch_id: str = "") -> List[Dict]:
        """Attendance report rows (delegates to the attendance service)."""
        return AttendanceService(self.db).report(course_id, batch_id)
