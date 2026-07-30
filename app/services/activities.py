"""Abandoned-activity tracking.

One table and one code path for every kind of unfinished work — tests,
assessments, psychometrics, mock interviews, pulse quizzes, capstone drafts,
moonshot days, profile completion — keyed by activity_type rather than a
separate tracker per feature.
"""
import logging
from datetime import datetime, timedelta
from typing import Dict, List, Optional

from sqlalchemy.orm import Session

from app.models import ActivityAttempt, Nudge
from app.services.copy import render
from app.services.messages import ABANDONED
from app.services.nudges import NudgeService

log = logging.getLogger("services.activities")

#: Escalation stages, most urgent first. `min_idle` is how long since the
#: student was last seen. Config, not control flow.
ABANDONMENT_STAGES = [
    {"stage": 3, "min_idle": timedelta(hours=24), "priority": "high", "severity": "warning"},
    {"stage": 2, "min_idle": timedelta(hours=2), "priority": "medium", "severity": "info"},
    {"stage": 1, "min_idle": timedelta(minutes=30), "priority": "low", "severity": "info"},
]

#: An attempt expiring inside this window jumps to stage 4 regardless of idle
#: time — a deadline beats a timer.
EXPIRY_URGENT_WINDOW = timedelta(hours=6)
EXPIRY_STAGE = {"stage": 4, "priority": "critical", "severity": "critical"}

#: Stop chasing an attempt this long after it was last touched. Beyond here it
#: is not abandoned, it is abandoned-and-forgotten, and nagging is noise.
CHASE_CUTOFF = timedelta(days=14)

#: Rough minutes per remaining step, used to say "about N minutes left".
#: Deliberately coarse — an honest estimate beats false precision.
MINUTES_PER_STEP = 0.5


def stage_for(idle: timedelta, time_to_expiry: Optional[timedelta]) -> Optional[Dict]:
    """Which escalation stage an abandoned attempt is in.

    Pure function — unit-testable without a database.

    Args:
        idle: how long since the student was last seen.
        time_to_expiry: time until the attempt closes, or None if it never does.

    Returns:
        A stage dict, or None when it is too soon to nudge.
    """
    if time_to_expiry is not None and timedelta(0) < time_to_expiry <= EXPIRY_URGENT_WINDOW:
        return EXPIRY_STAGE
    for candidate in ABANDONMENT_STAGES:
        if idle >= candidate["min_idle"]:
            return candidate
    return None


def minutes_remaining(steps_done: int, steps_total: int) -> int:
    """Rough minutes of work left. Returns 0 when the total is unknown."""
    remaining = max(0, (steps_total or 0) - (steps_done or 0))
    return max(1, round(remaining * MINUTES_PER_STEP)) if remaining else 0


class ActivityService:
    """Records activity attempts and chases the ones left unfinished."""

    def __init__(self, db: Session):
        self.db = db
        self.nudges = NudgeService(db)

    def start(
        self,
        user_id: str,
        activity_type: str,
        activity_id: str,
        course_id: str = "",
        activity_name: str = "",
        steps_total: int = 0,
        resume_url: str = "",
        expires_at: str = "",
    ) -> ActivityAttempt:
        """Open (or reopen) an attempt so it can be detected as abandoned."""
        attempt = self._find(user_id, activity_type, activity_id)
        now = datetime.utcnow()
        expiry = self._parse(expires_at)

        if attempt:
            attempt.last_seen_at = now
            attempt.completed = False
            attempt.completed_at = None
            if expiry:
                attempt.expires_at = expiry
        else:
            attempt = ActivityAttempt(
                user_id=user_id, course_id=course_id,
                activity_type=activity_type, activity_id=activity_id,
                activity_name=activity_name or activity_type.replace("_", " ").title(),
                steps_total=steps_total, resume_url=resume_url,
                started_at=now, last_seen_at=now, expires_at=expiry,
                completed=False, reminded_stage=0,
            )
            self.db.add(attempt)
        self.db.commit()
        return attempt

    def progress(
        self, user_id: str, activity_type: str, activity_id: str,
        steps_done: int = 0, progress_percent: int = 0,
    ) -> None:
        """Heartbeat. Keeps the attempt alive and records how far along it is."""
        attempt = self._find(user_id, activity_type, activity_id)
        if not attempt:
            log.info("Progress for untracked attempt %s/%s", activity_type, activity_id)
            return
        attempt.steps_done = max(attempt.steps_done or 0, steps_done)
        attempt.progress_percent = max(attempt.progress_percent or 0, progress_percent)
        if not attempt.progress_percent and attempt.steps_total:
            attempt.progress_percent = round(
                (attempt.steps_done / attempt.steps_total) * 100
            )
        attempt.last_seen_at = datetime.utcnow()
        # Progress means they came back — let the ladder start again.
        attempt.reminded_stage = 0
        self.db.commit()

    def complete(self, user_id: str, activity_type: str, activity_id: str) -> None:
        """Close the attempt. No further reminders."""
        attempt = self._find(user_id, activity_type, activity_id)
        if not attempt:
            return
        attempt.completed = True
        attempt.completed_at = datetime.utcnow()
        attempt.progress_percent = 100
        self.db.commit()

    def _find(self, user_id: str, activity_type: str, activity_id: str):
        """One attempt by its natural key."""
        return self.db.query(ActivityAttempt).filter(
            ActivityAttempt.user_id == user_id,
            ActivityAttempt.activity_type == activity_type,
            ActivityAttempt.activity_id == activity_id,
        ).first()

    @staticmethod
    def _parse(value: str) -> Optional[datetime]:
        """Parse an ISO timestamp, tolerating absence and malformed input."""
        if not value:
            return None
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).replace(tzinfo=None)
        except ValueError:
            log.warning("Unparseable expires_at %r", value)
            return None

    def sweep(self) -> List[Nudge]:
        """Cron: nudge every attempt that has gone quiet.

        Returns:
            The nudges created this run.
        """
        now = datetime.utcnow()
        open_attempts = self.db.query(ActivityAttempt).filter(
            ActivityAttempt.completed.is_(False),
            ActivityAttempt.last_seen_at > now - CHASE_CUTOFF,
        ).all()

        sent = [n for n in (self._chase(a, now) for a in open_attempts) if n]
        self.db.commit()
        return sent

    def _chase(self, attempt: ActivityAttempt, now: datetime) -> Optional[Nudge]:
        """Send one abandonment nudge if this attempt has earned a new stage."""
        idle = now - (attempt.last_seen_at or attempt.started_at or now)
        to_expiry = (attempt.expires_at - now) if attempt.expires_at else None
        stage = stage_for(idle, to_expiry)
        if not stage:
            return None
        # One nudge per stage. Escalating is allowed; repeating is not.
        if stage["stage"] <= (attempt.reminded_stage or 0):
            return None

        percent = attempt.progress_percent or 0
        message = render(ABANDONED, stage["stage"], {
            "activity": attempt.activity_name,
            "done": attempt.steps_done or 0,
            "total": attempt.steps_total or 0,
            "pct": percent,
            "minutes": minutes_remaining(attempt.steps_done, attempt.steps_total),
            "when": (attempt.last_seen_at or now).strftime("%A"),
            "hours": round(to_expiry.total_seconds() / 3600) if to_expiry else 0,
        }, nudge_type="activity_abandoned", escalation=stage["stage"])

        nudge = self.nudges.create(
            user_id=attempt.user_id, role="student",
            nudge_type="activity_abandoned",
            title=message["title"], body=message["body"],
            template_id=message["template_id"],
            severity=stage["severity"], priority=stage["priority"],
            cta_text=message["cta"], cta_url=attempt.resume_url,
            meta={
                "activity": attempt.activity_name,
                "activity_type": attempt.activity_type,
                "percent": percent,
                "hours": round(to_expiry.total_seconds() / 3600) if to_expiry else 0,
                "url": attempt.resume_url,
            },
            escalation=stage["stage"],
        )
        if nudge:
            attempt.reminded_stage = stage["stage"]
            attempt.last_reminded_at = now
        return nudge

    def open_for_student(self, user_id: str) -> List[Dict]:
        """Everything this student has left unfinished."""
        rows = self.db.query(ActivityAttempt).filter(
            ActivityAttempt.user_id == user_id,
            ActivityAttempt.completed.is_(False),
        ).order_by(ActivityAttempt.last_seen_at.desc()).all()
        return [{
            "type": r.activity_type, "name": r.activity_name,
            "percent": r.progress_percent or 0,
            "steps_done": r.steps_done or 0, "steps_total": r.steps_total or 0,
            "minutes_left": minutes_remaining(r.steps_done, r.steps_total),
            "resume_url": r.resume_url,
            "last_seen": str(r.last_seen_at) if r.last_seen_at else None,
            "expires_at": str(r.expires_at) if r.expires_at else None,
        } for r in rows]
