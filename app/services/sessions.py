"""Scheduled classes and the reminders that precede them.

The agent used to learn a lecture existed only when attendance was marked —
i.e. after it had already started. This is the timetable, and it is what makes
60/30/15-minute reminders possible.
"""
import logging
from datetime import datetime, timedelta
from typing import Dict, List, Optional

from sqlalchemy.orm import Session

from app.models import Nudge, ScheduledClass
from app.services.messages import CLASS_REMINDER, get_msg
from app.services.nudges import NudgeService

log = logging.getLogger("services.sessions")

#: Reminder tiers in minutes before the class, most urgent first.
#: Each tier fires at most once per class. Adding a tier is a data edit.
REMINDER_TIERS = [
    {"minutes": 15, "priority": "high"},
    {"minutes": 30, "priority": "high"},
    {"minutes": 60, "priority": "medium"},
]

#: How early a tier may fire. The sweep runs every 5 minutes, so a class 58
#: minutes away still counts as the 60-minute tier.
TIER_TOLERANCE_MINUTES = 6

#: Do not remind about a class that has already started.
MIN_LEAD_MINUTES = 1


def tier_for(minutes_until: float, already_sent: int) -> Optional[Dict]:
    """Which reminder tier is due, if any.

    Pure function — unit-testable without a database.

    Args:
        minutes_until: minutes until the class starts.
        already_sent: the most urgent tier already sent (0 if none).

    Returns:
        The tier dict, or None when nothing is due.

    Notes:
        Tiers are stored as "most urgent already sent", so a lower number means
        further along. 0 means nothing sent yet.
    """
    if minutes_until < MIN_LEAD_MINUTES:
        return None
    for tier in REMINDER_TIERS:
        window = tier["minutes"] + TIER_TOLERANCE_MINUTES
        if minutes_until <= window:
            if already_sent and tier["minutes"] >= already_sent:
                return None      # this tier or a more distant one already went
            return tier
    return None


class SessionService:
    """Registers scheduled classes and sends pre-class reminders."""

    def __init__(self, db: Session):
        self.db = db
        self.nudges = NudgeService(db)

    def schedule(
        self,
        class_id: str,
        course_id: str,
        starts_at: str,
        batch_id: str = "",
        title: str = "",
        duration_minutes: int = 60,
        join_url: str = "",
        mentor_id: str = "",
        student_ids: Optional[List[str]] = None,
    ) -> Optional[ScheduledClass]:
        """Register (or reschedule) a class so it can be reminded about."""
        when = self._parse(starts_at)
        if not when:
            log.warning("Class %s has no usable start time — not scheduled", class_id)
            return None

        existing = self.db.query(ScheduledClass).filter(
            ScheduledClass.class_id == class_id
        ).first()

        if existing:
            # A moved class deserves its reminders again.
            if existing.starts_at != when:
                existing.reminded_at_tier = 0
            existing.starts_at = when
            existing.title = title or existing.title
            existing.join_url = join_url or existing.join_url
            existing.mentor_id = mentor_id or existing.mentor_id
            existing.student_ids = student_ids or existing.student_ids
            existing.cancelled = False
            self.db.commit()
            return existing

        scheduled = ScheduledClass(
            class_id=class_id, course_id=course_id, batch_id=batch_id,
            title=title, starts_at=when, duration_minutes=duration_minutes,
            join_url=join_url, mentor_id=mentor_id,
            student_ids=student_ids or [], reminded_at_tier=0, cancelled=False,
        )
        self.db.add(scheduled)
        self.db.commit()
        log.info("Scheduled class %s at %s for %d students",
                 class_id, when, len(student_ids or []))
        return scheduled

    def cancel(self, class_id: str) -> bool:
        """Stop reminders for a cancelled class."""
        scheduled = self.db.query(ScheduledClass).filter(
            ScheduledClass.class_id == class_id
        ).first()
        if not scheduled:
            return False
        scheduled.cancelled = True
        self.db.commit()
        return True

    @staticmethod
    def _parse(value) -> Optional[datetime]:
        """Parse an ISO timestamp to naive UTC."""
        if isinstance(value, datetime):
            return value
        if not value:
            return None
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
            return parsed.replace(tzinfo=None) if parsed.tzinfo is None else (
                parsed.astimezone().replace(tzinfo=None)
            )
        except ValueError:
            log.warning("Unparseable starts_at %r", value)
            return None

    def send_reminders(self) -> List[Nudge]:
        """Cron: remind students about classes starting soon.

        Runs every few minutes. Each class fires at most one nudge per tier.

        Returns:
            The nudges created this run.
        """
        now = datetime.utcnow()
        horizon = now + timedelta(minutes=REMINDER_TIERS[-1]["minutes"] + TIER_TOLERANCE_MINUTES)

        upcoming = self.db.query(ScheduledClass).filter(
            ScheduledClass.cancelled.is_(False),
            ScheduledClass.starts_at > now,
            ScheduledClass.starts_at <= horizon,
        ).all()

        sent: List[Nudge] = []
        for scheduled in upcoming:
            sent.extend(self._remind_class(scheduled, now))
        self.db.commit()
        return sent

    def _remind_class(self, scheduled: ScheduledClass, now: datetime) -> List[Nudge]:
        """Send the due tier to every student on one class."""
        minutes_until = (scheduled.starts_at - now).total_seconds() / 60
        tier = tier_for(minutes_until, scheduled.reminded_at_tier or 0)
        if not tier:
            return []

        topic = scheduled.title or "your next class"
        message = get_msg(CLASS_REMINDER, tier["minutes"], {"topic": topic})
        created = []

        for user_id in (scheduled.student_ids or []):
            nudge = self.nudges.create(
                user_id=str(user_id), role="student", nudge_type="class_reminder",
                title=message["title"], body=message["body"],
                severity=message["severity"], priority=tier["priority"],
                cta_text=message["cta"], cta_url=scheduled.join_url,
                meta={
                    "class_id": scheduled.class_id,
                    "course": topic,
                    "minutes": tier["minutes"],
                    "url": scheduled.join_url,
                },
                escalation=tier["minutes"],
            )
            if nudge:
                created.append(nudge)

        scheduled.reminded_at_tier = tier["minutes"]
        log.info("Class %s: %d-minute reminder to %d students",
                 scheduled.class_id, tier["minutes"], len(created))
        return created

    def upcoming(self, course_id: str = "", limit: int = 50) -> List[Dict]:
        """Classes still to come, soonest first."""
        query = self.db.query(ScheduledClass).filter(
            ScheduledClass.cancelled.is_(False),
            ScheduledClass.starts_at > datetime.utcnow(),
        )
        if course_id:
            query = query.filter(ScheduledClass.course_id == course_id)
        rows = query.order_by(ScheduledClass.starts_at).limit(limit).all()
        return [{
            "class_id": r.class_id, "course_id": r.course_id,
            "title": r.title, "starts_at": str(r.starts_at),
            "students": len(r.student_ids or []),
            "reminded_at_tier": r.reminded_at_tier or 0,
        } for r in rows]
