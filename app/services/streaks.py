"""Attendance streaks and the recognition that goes with them."""
import logging
from datetime import datetime
from typing import Dict, List, Optional

from sqlalchemy.orm import Session

from app.models import AttendanceTracker, Nudge, Streak
from app.services.copy import render
from app.services.messages import STREAK
from app.services.nudges import NudgeService

log = logging.getLogger("services.streaks")

#: Consecutive classes that earn recognition, largest first.
#: Config, not control flow — add a 60 here and it just works.
MILESTONES = [
    {"classes": 30, "key": "month", "label": 30},
    {"classes": 7, "key": "week", "label": 7},
]


def milestone_for(current: int, already_celebrated: int) -> Optional[Dict]:
    """The milestone this streak has just reached, if any.

    Pure function. Returns None when the streak is below the first milestone,
    or when the highest one reached has already been celebrated — so a student
    on a 40-class streak is congratulated once at 30, not every single class.

    Args:
        current: consecutive classes attended.
        already_celebrated: the highest milestone previously awarded.

    Returns:
        The milestone dict, or None.
    """
    for milestone in MILESTONES:
        if current >= milestone["classes"] > already_celebrated:
            return milestone
    return None


class StreakService:
    """Maintains streaks and celebrates milestones."""

    def __init__(self, db: Session):
        self.db = db
        self.nudges = NudgeService(db)

    def record(
        self,
        user_id: str,
        course_id: str,
        attended: bool,
        course_name: str = "",
    ) -> Optional[Nudge]:
        """Update the streak after one attendance event.

        A miss resets the streak silently. **No broken-streak message is ever
        sent** — a student who just lost a month-long run already knows, and
        telling them turns a lapse into a reason to stop entirely.

        Returns:
            A celebration nudge if a milestone was reached, else None.
        """
        streak = self._get_or_create(user_id, course_id)
        now = datetime.utcnow()

        if not attended:
            if streak.current_classes:
                log.info(
                    "Streak reset for %s in %s at %d classes (silent)",
                    user_id, course_id, streak.current_classes,
                )
            streak.current_classes = 0
            streak.streak_started_at = None
            streak.last_milestone = 0
            self.db.commit()
            return None

        streak.current_classes = (streak.current_classes or 0) + 1
        streak.longest_classes = max(streak.longest_classes or 0, streak.current_classes)
        streak.last_attended_at = now
        if streak.current_classes == 1:
            streak.streak_started_at = now
        self.db.commit()

        milestone = milestone_for(streak.current_classes, streak.last_milestone or 0)
        if not milestone:
            return None

        streak.last_milestone = milestone["classes"]
        self.db.commit()
        return self._celebrate(streak, milestone, course_name or course_id)

    def _get_or_create(self, user_id: str, course_id: str) -> Streak:
        """This student's streak row for the course, created if absent."""
        streak = self.db.query(Streak).filter(
            Streak.user_id == user_id,
            Streak.course_id == course_id,
        ).first()
        if streak:
            return streak
        streak = Streak(user_id=user_id, course_id=course_id, current_classes=0)
        self.db.add(streak)
        self.db.flush()
        return streak

    def _celebrate(self, streak: Streak, milestone: Dict, course_name: str) -> Optional[Nudge]:
        """Send the milestone nudge, naming the number rather than gushing."""
        message = render(STREAK, milestone["key"], {
            "count": streak.current_classes,
            "course": course_name,
            "pct": self._attendance_pct(streak.user_id, streak.course_id),
        }, nudge_type="streak", escalation=milestone["label"])
        return self.nudges.create(
            user_id=streak.user_id, role="student", nudge_type="streak",
            title=message["title"], body=message["body"],
            template_id=message["template_id"],
            severity="success", priority="low", cta_text=message["cta"],
            cta_url=f"/courses/{streak.course_id}/attendance",
            meta={"milestone": milestone["classes"], "course_id": streak.course_id},
            escalation=milestone["label"],
        )

    def _attendance_pct(self, user_id: str, course_id: str) -> int:
        """Attendance percentage, or 0 when there is nothing to compute from."""
        tracker = self.db.query(AttendanceTracker).filter(
            AttendanceTracker.user_id == user_id,
            AttendanceTracker.course_id == course_id,
        ).first()
        if not tracker or not tracker.total_lectures:
            return 0
        return round(((tracker.attended_count or 0) / tracker.total_lectures) * 100)

    def leaderboard(self, course_id: str = "", limit: int = 20) -> List[Dict]:
        """Longest current streaks, for the mentor dashboard."""
        query = self.db.query(Streak).filter(Streak.current_classes > 0)
        if course_id:
            query = query.filter(Streak.course_id == course_id)
        rows = query.order_by(Streak.current_classes.desc()).limit(limit).all()
        return [{
            "user_id": r.user_id, "course_id": r.course_id,
            "current": r.current_classes, "longest": r.longest_classes,
            "since": str(r.streak_started_at) if r.streak_started_at else None,
        } for r in rows]
