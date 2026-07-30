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
        {"title": "We saved today’s class for you",
         "body": "Namaste {name}. Today’s class on {topic} is safely recorded for you. Every day of this course teaches one new tool — watch the recording today and you will not miss a thing.",
         "severity": "info", "cta": "Watch recording"},
        {"title": "Your {topic} recording is ready",
         "body": "Namaste {name}. We missed you in the {topic} class today. The full recording is ready — 30 to 40 minutes and you are fully caught up.",
         "severity": "info", "cta": "Watch recording"},
    ],
    2: [{"title": "Two tools are waiting for you",
         "body": "Namaste {name}. You have missed two classes, and each one taught a new tool. Both recordings are saved for you. Watch one today and one tomorrow — you will be back with the batch in no time.",
         "severity": "warning", "cta": "View recordings"}],
    3: [{"title": "Let us help you catch up",
         "body": "Namaste {name}. Three classes are waiting for you, and we do not want you to lose those tools. Your mentor knows and is happy to make a simple catch-up plan with you. One small step today is enough.",
         "severity": "critical", "cta": "Talk to mentor"}],
    5: [{"title": "We are missing you, {name}",
         "body": "Namaste {name}. Attendance is at {pct}% and several tool-days are waiting for you. Nothing is lost — every recording is saved, and your mentor will help you make a catch-up plan. You can still complete this course well.",
         "severity": "critical", "cta": "View attendance"}],
}

RECORDING = {
    "not_watched": [{"title": "Recording ready: {topic}",
                     "body": "Namaste. The recording of {topic} has been ready for {days} days. Watch it when you get some free time today — it keeps you fully up to date.",
                     "severity": "info", "cta": "Watch now"}],
    "partial": [{"title": "A little more of {topic} to go",
                 "body": "Namaste. You have watched {pct}% of {topic} — good going. A short sitting will finish it.",
                 "severity": "info", "cta": "Continue watching"}],
    "overdue": [{"title": "{topic} is waiting for you",
                 "body": "Namaste. The {topic} recording has been waiting {days} days. Watching it today keeps that tool in your hands before the course moves on.",
                 "severity": "warning", "cta": "Watch now"}],
    "multiple_pending": [{"title": "{count} recordings saved for you",
                          "body": "Namaste. {count} class recordings are saved and waiting. Start with the oldest one — 30 minutes a day clears them quickly.",
                          "severity": "warning", "cta": "View all"}],
}

ASSIGNMENT = {
    "not_viewed_48h": [{"title": "A new {type} is waiting",
                        "body": "Namaste. ‘{title}’ was shared {days} days ago. Please open it once today and see what is asked — starting early makes it easy.",
                        "severity": "info", "cta": "Open it"}],
    "3_days": [{"title": "3 days left for ‘{title}’",
                "body": "Namaste. Your {type} ‘{title}’ is due in 3 days. Start today with whatever you know — a good start now is worth more than a rush later.",
                "severity": "warning", "cta": "Start now"}],
    "1_day": [{"title": "‘{title}’ is due tomorrow",
               "body": "Namaste. Your ‘{title}’ closes tomorrow. Please submit what you have — even simple work submitted on time counts fully. We are waiting to see it.",
               "severity": "critical", "cta": "Submit now"}],
    "6_hours": [{"title": "{hours} hours left — you can do this",
                 "body": "Namaste. ‘{title}’ closes in {hours} hours. Whatever you have done so far, please submit it now — something submitted is always better than nothing. All the best!",
                 "severity": "critical", "cta": "Submit"}],
}

TOPIC = {
    "low": [{"title": "Let us make {topic} strong",
             "body": "Namaste. You scored {score}% in {topic} — thank you for attempting it. A short 15-minute revision will make this topic much clearer. Many students found it tricky the first time too.",
             "severity": "warning", "cta": "Start revision"}],
    "repeated": [{"title": "We will crack {topic} together",
                  "body": "Namaste {name}. {topic} has taken {attempts} attempts and that is completely okay — hard topics need more time. A short session with your mentor will clear it. Shall we book one?",
                  "severity": "warning", "cta": "Book session"}],
    "below_avg": [{"title": "A small push in {topic}",
                   "body": "Namaste. You are at {score}% in {topic} and the batch is at {avg}%. The gap is small — two focused revisions will close it. You are closer than you think.",
                   "severity": "warning", "cta": "Revise now"}],
    "improved": [{"title": "Great comeback in {topic}",
                  "body": "Namaste {name}. Your {topic} score went from {old}% to {score}%. That is real hard work showing. असेच पुढे चला!",
                  "severity": "success", "cta": "Keep going"}],
}

CLASS_REMINDER = {
    60: [{"title": "{topic} starts in one hour",
          "body": "Namaste. Today we learn {topic}. The joining link will be ready for you.",
          "severity": "info", "cta": "View class"}],
    30: [{"title": "{topic} starts in 30 minutes",
          "body": "Namaste. A good time to look at yesterday’s notes for two minutes.",
          "severity": "info", "cta": "Join class"}],
    15: [{"title": "Class starts in 15 minutes",
          "body": "{topic} is about to begin. See you there!",
          "severity": "info", "cta": "Join now"}],
}

ABANDONED = {
    1: [{"title": "Your {activity} is saved at {done} of {total}",
         "body": "Namaste. Your progress is safely saved. About {minutes} minutes of work is left — finish it whenever you are ready today.",
         "severity": "info", "cta": "Resume"}],
    2: [{"title": "{activity} is {pct}% done — well begun",
         "body": "Namaste. You have already done the harder part by starting. Picking it up now is easier than starting fresh tomorrow.",
         "severity": "info", "cta": "Resume"}],
    3: [{"title": "Your {activity} is waiting since {when}",
         "body": "Namaste. You are {pct}% through and everything is saved. About {minutes} minutes will finish it — you are very close.",
         "severity": "warning", "cta": "Finish it"}],
    4: [{"title": "{hours} hours left to finish {activity}",
         "body": "Namaste. Your {activity} closes in {hours} hours and your answers are safely saved at {pct}%. A short sitting now completes it — you can do this.",
         "severity": "critical", "cta": "Finish now"}],
}

STREAK = {
    "week": [{"title": "{count} days, no gaps",
              "body": "Namaste. A full week of {course} without missing a single day. छान चालले आहे! Keep it going.",
              "severity": "success", "cta": "View attendance"}],
    "month": [{"title": "Every single day — completed",
               "body": "Namaste. {count} days of {course}, attendance at {pct}%, not one day missed. अभिनंदन! आम्हाला तुमचा अभिमान आहे. This is how the course was meant to be done.",
               "severity": "success", "cta": "View attendance"}],
}

SCORE = {
    "exceptional": [{"title": "{score}% in {topic}",
                     "body": "Namaste. {delta} points above the batch average, on the hardest material in the module. उत्तम काम! Thank you for the effort you are putting in.",
                     "severity": "success", "cta": "See breakdown"}],
    "strong": [{"title": "{score}% in {topic}",
                "body": "Namaste. Comfortably above the pass mark — solid, steady work. Onwards!",
                "severity": "success", "cta": "See breakdown"}],
}

MENTOR = {
    "miss": [{"title": "Please check on {student}",
              "body": "{student} ({batch}) has missed {misses} classes in a row. Last active: {last_active}. A short call from you now can bring them back easily.",
              "severity": "critical", "cta": "Contact student"}],
    "unviewed": [{"title": "{count} students have not opened ‘{title}’",
                  "body": "‘{title}’ went out {days} days ago and {count} students have not opened it. Deadline in {left} days — a reminder from you carries weight.",
                  "severity": "warning", "cta": "Send reminder"}],
    "low_scores": [{"title": "{student} may need your help",
                    "body": "{student} scored below 50% on the last {count} quizzes. A short 1:1 session could make the difference.",
                    "severity": "warning", "cta": "Schedule session"}],
    "recordings": [{"title": "{count} students behind on recordings",
                    "body": "{count} students in {batch} have 3 or more unwatched recordings. A word from you in class will help them catch up.",
                    "severity": "warning", "cta": "View list"}],
    "dropout": [{"title": "High dropout risk: {student}",
                 "body": "Prediction: {prob}% dropout probability. Signals: {days} days inactive, scores declining. Please reach out personally — it works.",
                 "severity": "critical", "cta": "Reach out"}],
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