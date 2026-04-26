"""
PATCH NOTES (v2.1):
- get_msg now LOGS missing context keys instead of swallowing them silently.
  Previously a missing 'name' would leave '{name}' literally in the user-facing
  notification with no way to detect it.
- Added optional `seed` for reproducible nudge text in tests.
"""
import logging
import random
import string
from typing import Dict, Any, Optional

log = logging.getLogger("messages")

MISS = {
    1: [
        {"title": "Missed today\u2019s session",
         "body": "Hey {name}, you missed the {topic} session. Here\u2019s the recording so you don\u2019t fall behind!",
         "severity": "info", "cta": "Watch Recording"},
        {"title": "Session recording ready",
         "body": "{name}, missed {topic}? No worries \u2014 recording is ready. Your batchmates found it helpful!",
         "severity": "info", "cta": "Catch Up"},
    ],
    2: [{"title": "2 classes missed in a row",
         "body": "{name}, you\u2019ve missed 2 consecutive sessions. Tomorrow builds on what you missed. Catch up now.",
         "severity": "warning", "cta": "View Recordings"}],
    3: [{"title": "\u26A0 3 classes missed",
         "body": "{name}, 3 classes missed in a row. Your mentor {mentor} has been notified and wants to help. Schedule a catch-up call.",
         "severity": "critical", "cta": "Talk to Mentor"}],
    5: [{"title": "\U0001F6A8 Attendance critical",
         "body": "{name}, attendance at {pct}% this month. This may affect placement eligibility. Your institute has been notified.",
         "severity": "critical", "cta": "View Attendance"}],
}

RECORDING = {
    "not_watched": [{"title": "Recording available: {topic}",
                     "body": "The recording for \u2018{topic}\u2019 was uploaded {days} days ago. You haven\u2019t watched it yet. Stay on track!",
                     "severity": "info", "cta": "Watch Now"}],
    "partial": [{"title": "Finish watching: {topic}",
                 "body": "You watched {pct}% of \u2018{topic}.\u2019 Complete it to stay caught up with your batch.",
                 "severity": "info", "cta": "Continue"}],
    "overdue": [{"title": "\u23F0 Overdue recording: {topic}",
                 "body": "\u2018{topic}\u2019 was due {days} days ago and you haven\u2019t finished watching. Your batch has moved ahead.",
                 "severity": "warning", "cta": "Watch Now"}],
    "multiple_pending": [{"title": "{count} recordings pending",
                          "body": "You have {count} unwatched recordings. Falling behind makes catching up harder. Start with the oldest one.",
                          "severity": "warning", "cta": "View All"}],
}

ASSIGNMENT = {
    "not_viewed_48h": [{"title": "New {type} waiting",
                        "body": "\u2018{title}\u2019 was uploaded {days} days ago. You haven\u2019t opened it yet. See what\u2019s expected.",
                        "severity": "info", "cta": "Open"}],
    "3_days": [{"title": "\u23F0 Deadline in 3 days",
                "body": "Your {type} \u2018{title}\u2019 is due in 3 days. Not started yet. Submission closes permanently after deadline.",
                "severity": "warning", "cta": "Start Now"}],
    "1_day": [{"title": "\U0001F6A8 Due tomorrow!",
               "body": "URGENT: \u2018{title}\u2019 due TOMORROW. Portal closes permanently after deadline. Submit now \u2014 even partial work.",
               "severity": "critical", "cta": "Submit Now"}],
    "6_hours": [{"title": "\U0001F6A8 FINAL: {hours}h left!",
                 "body": "FINAL WARNING: {hours} hours to submit \u2018{title}.\u2019 Portal LOCKS after deadline. Act now.",
                 "severity": "critical", "cta": "Submit"}],
}

TOPIC = {
    "low": [{"title": "Let\u2019s strengthen {topic}",
             "body": "You scored {score}% on {topic}. Here\u2019s a 15-min revision to solidify your understanding.",
             "severity": "warning", "cta": "Start Revision"}],
    "repeated": [{"title": "{topic} needs help",
                  "body": "{name}, {attempts} attempts on {topic} and it\u2019s still tricky. Book a mentor doubt-clearing session.",
                  "severity": "warning", "cta": "Book Session"}],
    "below_avg": [{"title": "Gap in {topic}",
                   "body": "Batch average: {avg}%. You: {score}%. 2 focused sessions can close this gap.",
                   "severity": "warning", "cta": "Close Gap"}],
    "improved": [{"title": "\U0001F389 Great comeback in {topic}!",
                  "body": "{name}, your {topic} score jumped from {old}% to {score}%! Keep this momentum!",
                  "severity": "success", "cta": "Keep Going"}],
}

MENTOR = {
    "miss": [{"title": "\u26A0 Student missing classes",
              "body": "CRITICAL: {student} ({batch}) missed {misses} classes in a row. Last active: {last_active}. Reach out?",
              "severity": "critical", "cta": "Contact Student"}],
    "unviewed": [{"title": "Students haven\u2019t opened {type}",
                  "body": "{count} students haven\u2019t opened \u2018{title}\u2019 ({days} days ago). Deadline in {left} days.",
                  "severity": "warning", "cta": "Send Reminder"}],
    "low_scores": [{"title": "Student struggling: {student}",
                    "body": "{student} scored below 50% on last {count} quizzes. May need 1:1 help.",
                    "severity": "warning", "cta": "Schedule Session"}],
    "recordings": [{"title": "{count} students behind on recordings",
                    "body": "{count} students in {batch} have 3+ unwatched recordings. They\u2019re falling behind.",
                    "severity": "warning", "cta": "View List"}],
    "dropout": [{"title": "\U0001F6A8 HIGH dropout risk: {student}",
                 "body": "ML prediction: {prob}% dropout probability. Signals: {days}d inactive, scores declining. Intervene now.",
                 "severity": "critical", "cta": "Intervene"}],
}


def _required_keys(template_str: str):
    """Return set of placeholder keys in a format string."""
    return {fname for _, fname, _, _ in string.Formatter().parse(template_str) if fname}


def get_msg(templates: Dict[str, Any], key, ctx: Dict[str, Any],
            seed: Optional[int] = None) -> Dict[str, str]:
    tpls = templates.get(key)
    if not tpls:
        log.warning(f"Template key {key!r} not found, falling back to first")
        tpls = list(templates.values())[0]
    rnd = random.Random(seed) if seed is not None else random
    t = rnd.choice(tpls)

    out = {}
    for f in ("title", "body", "severity", "cta"):
        raw = t.get(f, "")
        if not isinstance(raw, str) or "{" not in raw:
            out[f] = raw
            continue
        needed = _required_keys(raw)
        missing = needed - set(ctx.keys())
        if missing:
            log.warning(
                f"Template field {f!r} missing context keys {missing} "
                f"(template={raw[:60]!r})"
            )
            # Fill missing with empty string so we don't leak literal '{name}' to users
            safe_ctx = {**{k: "" for k in missing}, **ctx}
        else:
            safe_ctx = ctx
        try:
            out[f] = raw.format(**safe_ctx)
        except Exception as e:
            log.error(f"Template render failed for {f!r}: {e}")
            out[f] = raw  # last resort
    return out