"""Unit tests for the v2.3 feature rules.

All pure functions — no database, no network.
"""
from datetime import timedelta

import pytest

from app.services.activities import minutes_remaining, stage_for as abandon_stage
from app.services.delivery import EXTRA_CHANNELS, WHATSAPP_TEMPLATES
from app.services.sessions import tier_for
from app.services.streaks import milestone_for
from app.services.topics import band_for


class TestScoreBands:
    """topics.band_for"""

    @pytest.mark.parametrize("score,key", [
        (100, "exceptional"), (96, "exceptional"), (95, "exceptional"),
        (94, "strong"), (85, "strong"),
        (49, "low"), (35, "low"),
        (34, "repeated"), (0, "repeated"),
    ])
    def test_bands(self, score, key):
        assert band_for(score)["key"] == key

    @pytest.mark.parametrize("score", [50, 60, 71, 84])
    def test_middle_band_is_silent(self, score):
        """A student who scored 71 does not need a notification about it."""
        assert band_for(score) is None

    def test_only_critical_alerts_the_mentor(self):
        assert band_for(28)["alert_mentor"] is True
        assert band_for(45)["alert_mentor"] is False
        assert band_for(96)["alert_mentor"] is False


class TestStreakMilestones:
    """streaks.milestone_for"""

    def test_below_first_milestone(self):
        assert milestone_for(6, 0) is None

    def test_week_and_month(self):
        assert milestone_for(7, 0)["key"] == "week"
        assert milestone_for(30, 7)["key"] == "month"

    def test_not_celebrated_twice(self):
        """A 40-class streak is congratulated once at 30, not every class."""
        assert milestone_for(40, 30) is None

    def test_month_wins_over_week(self):
        assert milestone_for(30, 0)["key"] == "month"


class TestClassReminderTiers:
    """sessions.tier_for"""

    @pytest.mark.parametrize("minutes,expected", [
        (58, 60), (30, 30), (14, 15), (3, 15),
    ])
    def test_tier_selected(self, minutes, expected):
        assert tier_for(minutes, 0)["minutes"] == expected

    def test_too_far_out(self):
        assert tier_for(120, 0) is None

    def test_already_started_gets_grace_when_nothing_was_sent(self):
        # Superseded rule: this used to assert silence. The at-least-one
        # guarantee now sends "you can still join" instead.
        got = tier_for(0.5, 0)
        assert got is not None and got["minutes"] == 0

    def test_already_started_stays_quiet_after_any_reminder(self):
        assert tier_for(0.5, 15) is None

    def test_does_not_repeat_a_tier(self):
        assert tier_for(58, 60) is None

    def test_escalates_to_a_tighter_tier(self):
        assert tier_for(14, 60)["minutes"] == 15


class TestAbandonmentStages:
    """activities.stage_for"""

    def test_too_soon(self):
        assert abandon_stage(timedelta(minutes=10), None) is None

    @pytest.mark.parametrize("idle_hours,stage", [
        (1, 1), (5, 2), (30, 3),
    ])
    def test_idle_ladder(self, idle_hours, stage):
        assert abandon_stage(timedelta(hours=idle_hours), None)["stage"] == stage

    def test_imminent_expiry_beats_idle_time(self):
        """A deadline outranks a timer — 40 minutes idle but closing in 2h."""
        got = abandon_stage(timedelta(minutes=40), timedelta(hours=2))
        assert got["stage"] == 4
        assert got["priority"] == "critical"

    def test_distant_expiry_does_not_escalate(self):
        got = abandon_stage(timedelta(hours=1), timedelta(days=3))
        assert got["stage"] == 1


class TestMinutesRemaining:
    def test_unknown_total(self):
        assert minutes_remaining(0, 0) == 0

    def test_finished(self):
        assert minutes_remaining(40, 40) == 0

    def test_partial(self):
        assert minutes_remaining(24, 40) == 8


class TestChannelPolicy:
    """delivery.EXTRA_CHANNELS — the routing contract."""

    def test_level_1_miss_never_emails(self):
        assert EXTRA_CHANNELS[("consecutive_miss", 1)] == ()

    def test_second_miss_emails_but_not_whatsapp(self):
        assert EXTRA_CHANNELS[("consecutive_miss", 2)] == ("email",)

    def test_fourth_miss_uses_every_channel(self):
        assert set(EXTRA_CHANNELS[("consecutive_miss", 4)]) == {"email", "whatsapp"}

    def test_class_reminders_never_email(self):
        for tier in (60, 30, 15):
            assert "email" not in EXTRA_CHANNELS[("class_reminder", tier)]

    def test_only_the_last_class_reminder_uses_whatsapp(self):
        assert EXTRA_CHANNELS[("class_reminder", 15)] == ("whatsapp",)
        assert EXTRA_CHANNELS[("class_reminder", 30)] == ()

    def test_every_whatsapp_route_has_an_approved_template(self):
        """Meta rejects unapproved templates, so the message would be lost."""
        for (nudge_type, _level), channels in EXTRA_CHANNELS.items():
            if "whatsapp" in channels:
                assert nudge_type in WHATSAPP_TEMPLATES, (
                    f"{nudge_type} routes to WhatsApp with no approved template"
                )

    def test_templates_declare_their_variables(self):
        for name, (template, variables) in WHATSAPP_TEMPLATES.items():
            assert template and isinstance(variables, tuple) and variables, name


class TestCopyPolicy:
    """copy.should_use_ai — the cost gate."""

    def test_ai_is_off_by_default(self):
        """Templates handle everything until AI is explicitly enabled."""
        from app.services.copy import should_use_ai
        assert should_use_ai("consecutive_miss", 4) is False

    def test_only_allowlisted_types_are_eligible(self, monkeypatch):
        import app.services.copy as copy_mod
        monkeypatch.setattr(copy_mod.settings, "enable_ai_copy", True, raising=False)
        assert copy_mod.should_use_ai("consecutive_miss", 4) is True
        # High-volume, perfectly templatable types must never spend a token.
        for cheap in ("class_reminder", "assignment_deadline", "streak",
                      "activity_abandoned", "score_exceptional"):
            assert copy_mod.should_use_ai(cheap, 4) is False, cheap

    def test_early_escalation_stays_on_templates(self, monkeypatch):
        import app.services.copy as copy_mod
        monkeypatch.setattr(copy_mod.settings, "enable_ai_copy", True, raising=False)
        assert copy_mod.should_use_ai("consecutive_miss", 1) is False
        assert copy_mod.should_use_ai("consecutive_miss", 2) is False
        assert copy_mod.should_use_ai("consecutive_miss", 3) is True


class TestCopyFallback:
    """copy.render — AI can never be the reason a nudge is missing."""

    def _templates(self):
        return {1: [{"title": "Missed {topic}", "body": "Recording is ready, {name}.",
                     "severity": "info", "cta": "Watch"}]}

    def test_template_path_substitutes_details(self):
        from app.services.copy import render
        got = render(self._templates(), 1, {"topic": "RAG", "name": "Aditya"})
        assert got["title"] == "Missed RAG"
        assert got["body"] == "Recording is ready, Aditya."
        assert got["template_id"].endswith(":1")

    def test_falls_back_when_ai_raises(self, monkeypatch):
        import app.services.copy as copy_mod
        monkeypatch.setattr(copy_mod.settings, "enable_ai_copy", True, raising=False)

        def boom(_result, _ctx):
            raise RuntimeError("model down")

        got = copy_mod.render(self._templates(), 1, {"topic": "RAG", "name": "A"},
                              nudge_type="consecutive_miss", escalation=4,
                              ai_writer=boom)
        assert got["title"] == "Missed RAG"      # template survived

    def test_falls_back_when_ai_returns_junk(self, monkeypatch):
        import app.services.copy as copy_mod
        monkeypatch.setattr(copy_mod.settings, "enable_ai_copy", True, raising=False)
        got = copy_mod.render(self._templates(), 1, {"topic": "RAG", "name": "A"},
                              nudge_type="consecutive_miss", escalation=4,
                              ai_writer=lambda r, c: {"title": "", "body": ""})
        assert got["title"] == "Missed RAG"

    def test_uses_ai_when_it_returns_something_usable(self, monkeypatch):
        import app.services.copy as copy_mod
        monkeypatch.setattr(copy_mod.settings, "enable_ai_copy", True, raising=False)
        got = copy_mod.render(self._templates(), 1, {"topic": "RAG", "name": "A"},
                              nudge_type="consecutive_miss", escalation=4,
                              ai_writer=lambda r, c: {"title": "Three weeks quiet",
                                                      "body": "Let's find a way back in."})
        assert got["title"] == "Three weeks quiet"
        assert got["template_id"].endswith(":ai")


class TestRoutingCoversTheLadder:
    """Every rung the attendance ladder can emit must have a routing entry."""

    def test_rung_five_reaches_email_and_whatsapp(self):
        # The ladder emits 1, 2, 3 and 5 — five-plus misses is the MOST severe
        # case and previously fell off the table entirely (dashboard-only).
        assert set(EXTRA_CHANNELS[("consecutive_miss", 5)]) == {"email", "whatsapp"}

    def test_every_ladder_rung_is_routable(self):
        from app.services.attendance import ESCALATION_LADDER
        for rung in ESCALATION_LADDER:
            key = ("consecutive_miss", rung["template_key"])
            assert key in EXTRA_CHANNELS, f"rung {rung['template_key']} unrouted"


class TestAtLeastOneReminder:
    """No class starts in silence — the just-started grace guarantees one."""

    def test_scheduled_8_minutes_before_gets_the_15_tier(self):
        assert tier_for(8, 0)["minutes"] == 15

    def test_scheduled_2_minutes_before_gets_the_15_tier(self):
        assert tier_for(2, 0)["minutes"] == 15

    def test_just_started_with_nothing_sent_gets_the_grace(self):
        got = tier_for(-3, 0)
        assert got is not None and got["minutes"] == 0

    def test_grace_never_fires_when_a_reminder_already_went(self):
        assert tier_for(-3, 15) is None

    def test_grace_expires_for_a_class_long_underway(self):
        assert tier_for(-25, 0) is None
