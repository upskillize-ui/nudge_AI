"""Send-policy tests: daily cap, quiet hours, dedup, and critical bypass.

Uses an in-memory SQLite database — no Aiven connection required.
"""
from datetime import datetime, timedelta
from unittest.mock import patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.models import Nudge
from app.services.nudges import NudgeService


@pytest.fixture()
def db():
    """A fresh in-memory database per test."""
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    yield session
    session.close()


def _send(service: NudgeService, priority="medium", nudge_type="consecutive_miss"):
    """Create one nudge with the given priority."""
    return service.create(
        user_id="stu_1", role="student", nudge_type=nudge_type,
        title="t", body="b", priority=priority,
    )


class TestDailyCap:
    def test_cap_blocks_routine_nudges(self, db):
        service = NudgeService(db)
        for i in range(8):
            assert _send(service, nudge_type=f"type_{i}") is not None
        assert _send(service, nudge_type="type_9") is None

    def test_critical_bypasses_the_cap(self, db):
        """A 3rd absence must not be dropped because the day was noisy."""
        service = NudgeService(db)
        for i in range(8):
            _send(service, nudge_type=f"type_{i}")
        assert _send(service, priority="critical", nudge_type="type_9") is not None


class TestQuietHours:
    def test_routine_nudge_suppressed_at_night(self, db):
        service = NudgeService(db)
        with patch.object(NudgeService, "_in_quiet_hours", return_value=True):
            assert _send(service) is None

    def test_critical_still_sends_at_night(self, db):
        service = NudgeService(db)
        with patch.object(NudgeService, "_in_quiet_hours", return_value=True):
            assert _send(service, priority="critical") is not None


class TestDedup:
    def test_same_type_within_window_is_suppressed(self, db):
        service = NudgeService(db)
        assert _send(service) is not None
        assert _send(service) is None

    def test_critical_does_not_bypass_dedup(self, db):
        """Bypassing volume limits is not a licence to send duplicates."""
        service = NudgeService(db)
        assert _send(service, priority="critical") is not None
        assert _send(service, priority="critical") is None

    def test_different_type_is_allowed(self, db):
        service = NudgeService(db)
        assert _send(service, nudge_type="consecutive_miss") is not None
        assert _send(service, nudge_type="assignment_deadline") is not None

    def test_allowed_again_after_window(self, db):
        service = NudgeService(db)
        first = _send(service)
        first.created_at = datetime.utcnow() - timedelta(hours=5)
        db.commit()
        assert _send(service) is not None


class TestPersistedFields:
    def test_nudge_is_pending_with_expiry(self, db):
        nudge = _send(NudgeService(db))
        assert nudge.status == "pending"
        assert nudge.expires_at > datetime.utcnow()
        assert db.query(Nudge).count() == 1


class TestStatusOwnership:
    """PATCH /nudges/{id}/status must not act on another user's nudge."""

    def _make(self, db):
        from app.services.nudges import NudgeService
        return NudgeService(db).create(
            user_id="owner-1", role="student", nudge_type="ownership_test",
            title="t", body="b",
        )

    def test_owner_can_update(self, db):
        from app.routes.feed import update_status
        from app.schemas import StatusUpdate
        nudge = self._make(db)
        got = update_status(nudge.id, StatusUpdate(status="read"), user_id="owner-1", db=db)
        assert got["ok"] is True

    def test_other_user_gets_404_not_403(self, db):
        """404 so the response never confirms the guessed id exists."""
        import pytest
        from fastapi import HTTPException
        from app.routes.feed import update_status
        from app.schemas import StatusUpdate
        nudge = self._make(db)
        with pytest.raises(HTTPException) as err:
            update_status(nudge.id, StatusUpdate(status="read"), user_id="intruder-9", db=db)
        assert err.value.status_code == 404

    def test_legacy_caller_without_user_id_still_works(self, db):
        """The deployed LMS does not send user_id yet — must not break it."""
        from app.routes.feed import update_status
        from app.schemas import StatusUpdate
        nudge = self._make(db)
        got = update_status(nudge.id, StatusUpdate(status="read"), user_id="", db=db)
        assert got["ok"] is True


class TestTemplateAttribution:
    """Every nudge records which copy variant produced it."""

    def test_template_id_is_persisted(self, db):
        from app.services.copy import render
        from app.services.messages import MISS
        from app.services.nudges import NudgeService
        message = render(MISS, 1, {"lecture_title": "RAG", "course_name": "CBAF",
                                   "attended_pct": 90, "name": "A"},
                         nudge_type="consecutive_miss", escalation=1)
        nudge = NudgeService(db).create(
            user_id="u1", role="student", nudge_type="consecutive_miss",
            title=message["title"], body=message["body"],
            template_id=message["template_id"],
        )
        assert nudge.template_id == "consecutive_miss:1"


class TestClassReminderDedupWindow:
    """Production bug: the 4h dedup let only the FIRST reminder tier through —
    tier 60 delivered, tiers 30/15/0 silently suppressed. class_reminder now
    has a 10-minute window so every tier lands while duplicate sweeps stay
    suppressed."""

    def test_class_reminder_has_short_window(self):
        from app.services.nudges import DEDUP_OVERRIDES, DEDUP_WINDOW
        assert DEDUP_OVERRIDES["class_reminder"] <= timedelta(minutes=15)
        assert DEDUP_OVERRIDES["class_reminder"] < DEDUP_WINDOW

    def test_next_tier_not_suppressed(self, db):
        """A tier-60 reminder 30 minutes ago must not block the tier-30 one,
        while a duplicate inside ten minutes IS still suppressed."""
        svc = NudgeService(db)
        db.add(Nudge(
            user_id="u1", user_role="student", nudge_type="class_reminder",
            priority="medium", title="t", body="b", severity="info",
            status="pending", scheduled_at=datetime.utcnow(),
            created_at=datetime.utcnow() - timedelta(minutes=30),
        ))
        db.commit()
        assert svc._sent_recently("u1", "class_reminder") is False

        db.add(Nudge(
            user_id="u2", user_role="student", nudge_type="class_reminder",
            priority="medium", title="t", body="b", severity="info",
            status="pending", scheduled_at=datetime.utcnow(),
            created_at=datetime.utcnow() - timedelta(minutes=2),
        ))
        db.commit()
        assert svc._sent_recently("u2", "class_reminder") is True
