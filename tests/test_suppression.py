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
