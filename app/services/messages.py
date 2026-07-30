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
        {"title": "You missed today\u2019s class: {topic}",
         "body": "Hi {name}. Today\u2019s class taught {topic}, and you were not there \u2014 that is one tool the course has covered without you. The recording is saved. Watch it today, and the day is recovered.",
         "severity": "info", "cta": "Watch recording"},
        {"title": "One class missed \u2014 {topic}",
         "body": "Hi {name}. You missed {topic} today, so that tool is pending for you. The full recording is ready \u2014 about one sitting, and you have what the class has.",
         "severity": "info", "cta": "Watch recording"},
    ],
    2: [{"title": "Two classes missed \u2014 two tools pending",
         "body": "Hi {name}. You have missed two classes in a row, which means two tools you do not have yet. Both recordings are saved. One today, one tomorrow \u2014 and you are level again.",
         "severity": "warning", "cta": "View recordings"}],
    3: [{"title": "Three classes missed \u2014 time for a plan",
         "body": "Hi {name}. Three classes have now gone by without you \u2014 three tools pending, and each day adds one more. This is the point where a plan beats willpower. Your mentor already knows and is ready to make one with you. Start with one recording today.",
         "severity": "critical", "cta": "Talk to mentor"}],
    5: [{"title": "{misses} classes missed \u2014 here is where you stand",
         "body": "Hi {name}. Attendance is at {pct}%, and several tool-days are pending. All of them are recoverable \u2014 every recording is saved \u2014 but only if the catch-up starts now. Your mentor is ready to plan it with you. Today is the right day to begin.",
         "severity": "critical", "cta": "View attendance"}],
}

RECORDING = {
    "not_watched": [{"title": "Recording ready: {topic}",
                     "body": "Hi. The recording of {topic} has been ready for {days} days. Watch it when you get some free time today — it keeps you fully up to date.",
                     "severity": "info", "cta": "Watch now"}],
    "partial": [{"title": "A little more of {topic} to go",
                 "body": "Hi. You have watched {pct}% of {topic} — good going. A short sitting will finish it.",
                 "severity": "info", "cta": "Continue watching"}],
    "overdue": [{"title": "{topic} is waiting for you",
                 "body": "Hi. The {topic} recording has been waiting {days} days. Watching it today keeps that tool in your hands before the course moves on.",
                 "severity": "warning", "cta": "Watch now"}],
    "multiple_pending": [{"title": "{count} recordings saved for you",
                          "body": "Hi. {count} class recordings are saved and waiting. Start with the oldest one — 30 minutes a day clears them quickly.",
                          "severity": "warning", "cta": "View all"}],
}

ASSIGNMENT = {
    "not_viewed_48h": [{"title": "A new {type} is waiting",
                        "body": "Hi. ‘{title}’ was shared {days} days ago. Please open it once today and see what is asked — starting early makes it easy.",
                        "severity": "info", "cta": "Open it"}],
    "3_days": [{"title": "3 days left for ‘{title}’",
                "body": "Hi. Your {type} ‘{title}’ is due in 3 days. Start today with whatever you know — a good start now is worth more than a rush later.",
                "severity": "warning", "cta": "Start now"}],
    "1_day": [{"title": "‘{title}’ is due tomorrow",
               "body": "Hi. Your ‘{title}’ closes tomorrow. Please submit what you have — even simple work submitted on time counts fully. We are waiting to see it.",
               "severity": "critical", "cta": "Submit now"}],
    "6_hours": [{"title": "{hours} hours left — you can do this",
                 "body": "Hi. ‘{title}’ closes in {hours} hours. Whatever you have done so far, please submit it now — something submitted is always better than nothing. All the best!",
                 "severity": "critical", "cta": "Submit"}],
}

TOPIC = {
    "low": [{"title": "Let us make {topic} strong",
             "body": "Hi. You scored {score}% in {topic} — thank you for attempting it. A short 15-minute revision will make this topic much clearer — hard topics simply take a second pass, and the second pass is always easier.",
             "severity": "warning", "cta": "Start revision"}],
    "repeated": [{"title": "We will crack {topic} together",
                  "body": "Hi {name}. {topic} has taken {attempts} attempts and that is completely okay — hard topics need more time. A short session with your mentor will clear it. Shall we book one?",
                  "severity": "warning", "cta": "Book session"}],
    "improved": [{"title": "Great comeback in {topic}",
                  "body": "Hi {name}. Your {topic} score went from {old}% to {score}%. That is real hard work showing. शाब्बास!",
                  "severity": "success", "cta": "Keep going"}],
}

CLASS_REMINDER = {
    0: [{"title": "Your class has just started",
         "body": "Hi. {topic} is live right now \u2014 you can still join and catch almost everything. Come in!",
         "severity": "info", "cta": "Join now"}],
    60: [{"title": "{topic} starts in one hour",
          "body": "Hi. Today we learn {topic}. The joining link will be ready for you.",
          "severity": "info", "cta": "View class"}],
    30: [{"title": "{topic} starts in 30 minutes",
          "body": "Hi. A good time to look at yesterday’s notes for two minutes.",
          "severity": "info", "cta": "Join class"}],
    15: [{"title": "Class starts in 15 minutes",
          "body": "{topic} is about to begin. See you there!",
          "severity": "info", "cta": "Join now"}],
}

ABANDONED = {
    1: [{"title": "{activity} is waiting to be finished",
         "body": "Hi. {activity} was started and then left partway. The effort already given pays off only when it is finished — a small sitting today completes it.",
         "severity": "info", "cta": "Resume"}],
    2: [{"title": "{activity} is {pct}% done — well begun",
         "body": "Hi. You have already done the harder part by starting. Picking it up now is easier than starting fresh tomorrow.",
         "severity": "info", "cta": "Resume"}],
    3: [{"title": "{activity} has been waiting since {when}",
         "body": "Hi. You are {pct}% through — what remains is smaller than what you have already done. One focused sitting finishes it.",
         "severity": "warning", "cta": "Finish it"}],
    4: [{"title": "{hours} hours left to finish {activity}",
         "body": "Hi. {activity} closes in {hours} hours and you are at {pct}%. A short sitting now completes it — you can do this.",
         "severity": "critical", "cta": "Finish now"}],
}

#: Interviews cannot be resumed midway — an abandoned session means a fresh
#: start, and the value (score, feedback, report) only exists at the end.
#: Saying "your progress is saved" here would be a lie, so they get their
#: own honest copy.
ABANDONED_INTERVIEW = {
    1: [{"title": "Your mock interview stopped midway",
         "body": "Hi. Today's interview ended before the debrief — and the debrief is where the learning lives: your score, your feedback, your next steps. A fresh session gets you the full report. Ready when you are.",
         "severity": "info", "cta": "Start a fresh interview"}],
    2: [{"title": "That interview is still unfinished",
         "body": "Hi. An interview pays you back at the end — the report, the scores, what to fix next. Your last session stopped before that point. One complete sitting today gives you what the half one could not.",
         "severity": "info", "cta": "Start again"}],
    3: [{"title": "One finished interview beats three half ones",
         "body": "Hi. Your last mock interview, from {when}, was left midway. Finishing strong is itself an interview skill — practise exactly that. Fifteen quiet minutes, one complete session, full feedback.",
         "severity": "warning", "cta": "Start a fresh interview"}],
    4: [{"title": "Finish one interview today",
         "body": "Hi. A complete mock interview — start to debrief — is the single fastest way to improve. Your last one stopped early. Give the next one its full fifteen minutes.",
         "severity": "warning", "cta": "Start now"}],
}

STREAK = {
    "week": [{"title": "{count} days, no gaps",
              "body": "Hi. A full week of {course} without missing a single day. शाब्बास! Keep it going.",
              "severity": "success", "cta": "View attendance"}],
    "month": [{"title": "Every single day — completed",
               "body": "Hi. {count} days of {course}, attendance at {pct}%, not one day missed. अभिनंदन! This is how the course was meant to be done.",
               "severity": "success", "cta": "View attendance"}],
}

SCORE = {
    "exceptional": [{"title": "{score}% in {topic}",
                     "body": "Hi. A top score on the hardest material in the module so far. उत्तम काम! Thank you for the effort you are putting in.",
                     "severity": "success", "cta": "See breakdown"}],
    "strong": [{"title": "{score}% in {topic}",
                "body": "Hi. Comfortably above the pass mark — solid, steady work. Onwards!",
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