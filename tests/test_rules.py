"""Unit tests for the pure rule functions.

These need no database and no network — that is the whole point of having
extracted them out of the old engine.
"""
import pytest

from app.services.assignments import stage_for
from app.services.attendance import rung_for
from app.services.recordings import reminder_key
from app.services.topics import trend_for
from app.utils.timezone import is_within_window


class TestEscalationLadder:
    """attendance.rung_for"""

    def test_no_rung_below_first_miss(self):
        assert rung_for(0) is None

    @pytest.mark.parametrize("misses,priority,alerts", [
        (1, "medium", False),
        # Mentor joins at 2 misses — in a 30-day course, waiting for 3 means
        # 10% of the course is gone before a human hears about it.
        (2, "high", True),
        (3, "critical", True),
        (5, "critical", True),
    ])
    def test_rung_matches_threshold(self, misses, priority, alerts):
        rung = rung_for(misses)
        assert rung["priority"] == priority
        assert rung["alert_mentor"] is alerts

    def test_between_rungs_uses_lower(self):
        assert rung_for(4)["template_key"] == 3

    def test_far_above_top_rung_stays_at_top(self):
        assert rung_for(50)["template_key"] == 5


class TestDeadlineStages:
    """assignments.stage_for"""

    @pytest.mark.parametrize("hours_left,key", [
        (2, "6_hours"),
        (6, "6_hours"),
        (20, "1_day"),
        (48, "3_days"),
    ])
    def test_urgency_stages(self, hours_left, key):
        assert stage_for(hours_left, viewed=True, hours_since_upload=100)["template_key"] == key

    def test_distant_and_viewed_sends_nothing(self):
        assert stage_for(200, viewed=True, hours_since_upload=100) is None

    def test_distant_but_unopened_after_48h_nudges(self):
        stage = stage_for(200, viewed=False, hours_since_upload=49)
        assert stage["template_key"] == "not_viewed_48h"

    def test_unopened_but_only_just_published_waits(self):
        assert stage_for(200, viewed=False, hours_since_upload=10) is None


class TestRecordingReminders:
    """recordings.reminder_key"""

    def test_partial_watch_asks_them_to_finish(self):
        assert reminder_key(watch_percent=40, days_overdue=1) == "partial"

    def test_untouched_and_recent(self):
        assert reminder_key(watch_percent=0, days_overdue=1) == "not_watched"

    def test_untouched_and_long_overdue(self):
        assert reminder_key(watch_percent=0, days_overdue=9) == "overdue"


class TestScoreTrend:
    """topics.trend_for"""

    def test_first_attempt_is_flat(self):
        assert trend_for(None, 55) == "flat"

    @pytest.mark.parametrize("previous,current,expected", [
        (40, 60, "up"),
        (60, 40, "down"),
        (50, 55, "flat"),
        (50, 45, "flat"),
    ])
    def test_symmetric_band(self, previous, current, expected):
        assert trend_for(previous, current) == expected

    def test_band_edges_are_not_movement(self):
        assert trend_for(50, 60) == "flat"
        assert trend_for(50, 40) == "flat"


class TestQuietHoursWindow:
    """utils.timezone.is_within_window"""

    @pytest.mark.parametrize("hour,expected", [
        (23, True), (2, True), (6, True),
        (7, False), (12, False), (21, False),
    ])
    def test_window_wrapping_midnight(self, hour, expected):
        assert is_within_window(hour, 22, 7) is expected

    def test_window_within_one_day(self):
        assert is_within_window(10, 9, 17) is True
        assert is_within_window(18, 9, 17) is False
