"""Channel routing: which nudges leave the dashboard, and by which route.

DESIGN — the agent decides, the LMS delivers.

This service never talks to Meta or an SMTP server. It marks a nudge as queued
for email and/or WhatsApp; the LMS drains the outbox and sends using the
WhatsApp Business and mailer integrations it already owns. That keeps provider
credentials in one place, avoids a second WhatsApp number, and means a provider
outage never blocks nudge creation.

THE GOVERNING RULE
    Dashboard on every nudge, without exception.
    Email only where the routing table says so.
    WhatsApp only when it is genuinely time-critical — and only via an
    approved template, because business-initiated WhatsApp cannot be free-form.
"""
import logging
from datetime import datetime
from typing import Dict, List, Optional, Tuple

from sqlalchemy.orm import Session

from app.models import Contact, Nudge

log = logging.getLogger("services.delivery")

#: Extra channels beyond the dashboard, keyed by (nudge_type, level).
#: Level is the escalation level for ladders, or the stage for timed events.
#: Anything absent from this table is dashboard-only — which is most things.
EXTRA_CHANNELS: Dict[Tuple[str, int], Tuple[str, ...]] = {
    # Live class reminders — never email. Too slow for 15 minutes, too noisy
    # for 60. WhatsApp only at the last call.
    ("class_reminder", 60): (),
    ("class_reminder", 30): (),
    ("class_reminder", 15): ("whatsapp",),

    # Attendance ladder. Level 1 never emails — one missed class is normal
    # life, and emailing it spends the credibility needed at level 3.
    ("consecutive_miss", 1): (),
    ("consecutive_miss", 2): ("email",),
    ("consecutive_miss", 3): ("email",),
    ("consecutive_miss", 4): ("email", "whatsapp"),

    # Coursework deadlines, by hours remaining.
    ("assignment_deadline", 72): (),
    ("assignment_deadline", 24): ("email",),
    ("assignment_deadline", 6): ("email", "whatsapp"),
    ("coursework_missed", 0): ("email", "whatsapp"),

    # Abandoned attempts, by stage.
    ("activity_abandoned", 1): (),
    ("activity_abandoned", 2): (),
    ("activity_abandoned", 3): ("email",),
    ("activity_abandoned", 4): ("email", "whatsapp"),

    # Recognition.
    ("streak", 7): (),
    ("streak", 30): ("email",),
    ("score_exceptional", 0): ("email",),
    ("score_critical", 0): ("email",),
    ("certificate_unlocked", 0): ("email", "whatsapp"),
}

#: Approved WhatsApp templates. Business-initiated messages MUST use one of
#: these — Meta rejects free-form. Submit as UTILITY, not marketing.
#: Maps nudge_type -> (template_name, ordered metadata keys for variables).
WHATSAPP_TEMPLATES: Dict[str, Tuple[str, Tuple[str, ...]]] = {
    "class_reminder": ("class_starting_soon", ("course", "minutes", "url")),
    "assignment_deadline": ("deadline_final_hours", ("title", "hours", "url")),
    "coursework_missed": ("deadline_missed", ("title", "date", "mentor")),
    "activity_abandoned": ("activity_expiring", ("activity", "hours", "percent", "url")),
    "consecutive_miss": ("attendance_critical", ("misses", "course", "pct")),
    "certificate_unlocked": ("certificate_ready", ("certificate", "url")),
}

#: Max characters per WhatsApp template variable.
WA_VAR_LIMIT = 40

QUEUED, SKIPPED, NONE = "queued", "skipped", "none"


class DeliveryService:
    """Decides and records which channels a nudge should leave by."""

    def __init__(self, db: Session):
        self.db = db

    def route(self, nudge: Nudge, level: int = 0) -> List[str]:
        """Mark a nudge for the channels it qualifies for.

        Dashboard delivery is implicit — the nudge row itself is the dashboard.
        This only decides the extra channels.

        Args:
            nudge: the persisted Nudge.
            level: escalation level or stage, matching EXTRA_CHANNELS.

        Returns:
            The channels queued, e.g. ["email", "whatsapp"].
        """
        wanted = EXTRA_CHANNELS.get((nudge.nudge_type, level), ())
        if not wanted:
            return []

        contact = self.db.query(Contact).filter(Contact.user_id == nudge.user_id).first()
        queued = []

        if "email" in wanted:
            if self._email_allowed(contact):
                nudge.email_status = QUEUED
                queued.append("email")
            else:
                nudge.email_status = SKIPPED

        if "whatsapp" in wanted:
            template = self._whatsapp_template(nudge)
            if template and self._whatsapp_allowed(contact):
                nudge.whatsapp_status = QUEUED
                nudge.whatsapp_template = template
                queued.append("whatsapp")
            else:
                nudge.whatsapp_status = SKIPPED

        if queued:
            self.db.commit()
            log.info("Routed %s -> %s via %s", nudge.nudge_type, nudge.user_id, queued)
        return queued

    @staticmethod
    def _email_allowed(contact: Optional[Contact]) -> bool:
        """Consent gate. No contact row means no consent — never assume it."""
        if not contact or contact.unsubscribed_all:
            return False
        return bool(contact.email and contact.email_opt_in)

    @staticmethod
    def _whatsapp_allowed(contact: Optional[Contact]) -> bool:
        if not contact or contact.unsubscribed_all:
            return False
        return bool(contact.phone_e164 and contact.whatsapp_opt_in)

    @staticmethod
    def _whatsapp_template(nudge: Nudge) -> str:
        """Approved template for this nudge type, or "" if none fits.

        Never invents a template name — an unapproved name is rejected by Meta
        and the message is simply lost.
        """
        entry = WHATSAPP_TEMPLATES.get(nudge.nudge_type)
        return entry[0] if entry else ""

    def outbox(self, channel: str, limit: int = 50) -> List[Dict]:
        """Queued items for the LMS to send.

        Args:
            channel: "email" or "whatsapp".
            limit: max items.

        Returns:
            Payloads carrying everything the sender needs, including the
            recipient's address — resolved here so the LMS never has to
            reverse-map a user id.
        """
        if channel == "email":
            rows = self.db.query(Nudge).filter(Nudge.email_status == QUEUED)
        elif channel == "whatsapp":
            rows = self.db.query(Nudge).filter(Nudge.whatsapp_status == QUEUED)
        else:
            return []

        items = []
        for nudge in rows.order_by(Nudge.created_at).limit(limit).all():
            contact = self.db.query(Contact).filter(
                Contact.user_id == nudge.user_id
            ).first()
            if not contact:
                continue
            items.append(
                self._email_payload(nudge, contact) if channel == "email"
                else self._whatsapp_payload(nudge, contact)
            )
        return items

    @staticmethod
    def _email_payload(nudge: Nudge, contact: Contact) -> Dict:
        """One email for the LMS mailer to render into the brand wrapper."""
        return {
            "nudge_id": nudge.id,
            "to": contact.email,
            "first_name": (contact.full_name or "").split(" ")[0],
            "subject": nudge.title,
            "body": nudge.body,
            "cta_text": nudge.cta_text,
            "cta_url": nudge.cta_url,
            "severity": nudge.severity,
        }

    @staticmethod
    def _whatsapp_payload(nudge: Nudge, contact: Contact) -> Dict:
        """One templated WhatsApp message, variables in template order."""
        entry = WHATSAPP_TEMPLATES.get(nudge.nudge_type)
        meta = nudge.metadata_json or {}
        variables = []
        if entry:
            variables = [
                str(meta.get(key, ""))[:WA_VAR_LIMIT] for key in entry[1]
            ]
        return {
            "nudge_id": nudge.id,
            "to": contact.phone_e164,
            "template_name": nudge.whatsapp_template,
            "variables": variables,
        }

    def mark_sent(self, nudge_id: str, channel: str, ok: bool = True) -> bool:
        """Record the outcome of a send so it is not attempted again."""
        nudge = self.db.query(Nudge).filter(Nudge.id == nudge_id).first()
        if not nudge:
            return False
        status = "sent" if ok else "failed"
        now = datetime.utcnow()
        if channel == "email":
            nudge.email_status = status
            nudge.email_sent_at = now if ok else None
        elif channel == "whatsapp":
            nudge.whatsapp_status = status
            nudge.whatsapp_sent_at = now if ok else None
        else:
            return False
        self.db.commit()
        return True

    def upsert_contact(self, **fields) -> Contact:
        """Create or update someone's contact record and consent flags."""
        contact = self.db.query(Contact).filter(
            Contact.user_id == fields["user_id"]
        ).first()
        if not contact:
            contact = Contact(user_id=fields["user_id"])
            self.db.add(contact)
        for key, value in fields.items():
            if key != "user_id" and hasattr(contact, key):
                setattr(contact, key, value)
        contact.updated_at = datetime.utcnow()
        self.db.commit()
        return contact
