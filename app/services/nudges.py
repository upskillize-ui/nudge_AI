"""Nudge creation and suppression policy.

This is the single place a Nudge row is written. Every rule service goes
through `create()` so the daily cap, quiet hours and dedup window are
applied uniformly — there is no second path that bypasses them.
"""
import logging
from datetime import datetime, timedelta
from typing import Any, Dict, Optional

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import Nudge
from app.utils.timezone import ist_today_start_utc, is_within_window, now_ist

log = logging.getLogger("services.nudges")
settings = get_settings()

#: How long the same (user, nudge_type) pair is suppressed after a send.
DEDUP_WINDOW = timedelta(hours=4)

#: How long a pending nudge stays live before the expiry job retires it.
NUDGE_TTL = timedelta(days=3)


class NudgeService:
    """Creates nudges, enforcing all send policy in one place."""

    def __init__(self, db: Session):
        self.db = db

    def create(
        self,
        user_id: str,
        role: str,
        nudge_type: str,
        title: str,
        body: str,
        severity: str = "info",
        priority: str = "medium",
        cta_text: str = "",
        cta_url: str = "",
        meta: Optional[Dict[str, Any]] = None,
        escalation: int = 0,
    ) -> Optional[Nudge]:
        """Create a nudge unless send policy suppresses it.

        Args:
            user_id: Recipient id.
            role: "student" or "mentor".
            nudge_type: Machine key, e.g. "consecutive_miss".
            title: Notification title.
            body: Notification body.
            severity: info | warning | critical | success.
            priority: low | medium | high | critical.
            cta_text: Button label.
            cta_url: Button target.
            meta: Arbitrary JSON payload stored with the nudge.
            escalation: Escalation level, 0-4.

        Returns:
            The persisted Nudge, or None if suppressed.
        """
        reason = self._suppression_reason(user_id, nudge_type, priority)
        if reason:
            log.info("Suppressed %s -> %s: %s", nudge_type, user_id, reason)
            return None

        now = datetime.utcnow()
        nudge = Nudge(
            user_id=user_id, user_role=role, nudge_type=nudge_type,
            priority=priority, title=title, body=body,
            cta_text=cta_text, cta_url=cta_url, severity=severity,
            metadata_json=meta or {}, status="pending",
            scheduled_at=now, expires_at=now + NUDGE_TTL,
            escalation_level=escalation,
        )
        self.db.add(nudge)
        self.db.commit()
        self.db.refresh(nudge)
        log.info("Nudge: %s -> %s [%s]", nudge_type, user_id, priority)
        return nudge

    def _suppression_reason(self, user_id: str, nudge_type: str, priority: str) -> Optional[str]:
        """Return why this nudge should be suppressed, or None to allow it.

        Critical nudges bypass both the daily cap and quiet hours: a 3rd
        consecutive absence or a 6-hour deadline is exactly the message that
        must not be dropped because the student already saw eight others.
        Dedup still applies to everything — bypassing volume limits is not a
        licence to send the same nudge twice.
        """
        if priority != "critical":
            if self._daily_count(user_id) >= settings.max_nudges_per_day:
                return "daily cap"
            if self._in_quiet_hours():
                return "quiet hours"
        if self._sent_recently(user_id, nudge_type):
            return "4h dedup"
        return None

    def _daily_count(self, user_id: str) -> int:
        """Nudges created for this user since midnight IST."""
        count = self.db.query(func.count(Nudge.id)).filter(
            Nudge.user_id == user_id,
            Nudge.created_at >= ist_today_start_utc(),
        ).scalar()
        return count or 0

    def _in_quiet_hours(self) -> bool:
        """Whether the current IST hour falls inside the quiet window."""
        return is_within_window(
            now_ist().hour, settings.quiet_hours_start, settings.quiet_hours_end
        )

    def _sent_recently(self, user_id: str, nudge_type: str) -> bool:
        """Whether an identical nudge type went out inside the dedup window."""
        cutoff = datetime.utcnow() - DEDUP_WINDOW
        return self.db.query(Nudge.id).filter(
            Nudge.user_id == user_id,
            Nudge.nudge_type == nudge_type,
            Nudge.created_at > cutoff,
        ).first() is not None
