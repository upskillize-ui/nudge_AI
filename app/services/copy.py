"""Copy generation policy: templates by default, AI only where it earns its cost.

THE RULE
    Rules and templates produce the copy. AI is the exception, not the default.

Roughly 95% of nudges are the same handful of sentences with different details
substituted in — "your class starts in N minutes", "N hours left", "you are N%
through". A template does that perfectly, instantly, for zero cost, and reads
identically every time, which is what makes a notification feel like a system
rather than a slot machine.

AI is reserved for the few messages where the *wording itself* has to change
with the situation, not just the numbers: talking to a student who has gone
quiet for weeks, coaching someone failing the same topic repeatedly, or
drafting outreach a mentor will actually send. There, a generic sentence is
worse than no sentence.

COST SHAPE
    ~95% of volume  -> templates       -> zero
    ~5%  of volume   -> one small call  -> fractions of a paisa each

SAFETY
    Every AI path has a template fallback. If the model is disabled, out of
    budget, slow, or returns something unusable, the template is used and the
    student still gets a good nudge. AI can never be the reason a nudge is
    missing.
"""
import logging
from typing import Any, Callable, Dict, Optional

from app.config import get_settings
from app.services.messages import get_msg

log = logging.getLogger("services.copy")
settings = get_settings()

#: The ONLY nudge types allowed to spend a token. Everything absent from this
#: set is template-rendered, always. Adding a type here is a cost decision —
#: make it deliberately, and only when a template genuinely cannot do the job.
AI_ELIGIBLE_TYPES = frozenset({
    # A student three or more classes gone needs a message written to their
    # situation, not a form letter. This is the highest-stakes copy we send.
    "consecutive_miss",
    # Same topic failed twice. The useful part is *what to do about it*, which
    # depends on the topic and cannot be pre-written.
    "score_critical",
    # Faculty-facing outreach drafts. A mentor will paste this to a student, so
    # generic phrasing wastes the mentor's credibility, not ours.
    "mentor_alert",
    # Coming back after a lapse. Warmth here is specific or it is nothing.
    "returned_after_lapse",
})

#: Even inside the allowlist, only escalated cases justify a call. A first
#: missed class is template territory; a third is not.
AI_MIN_ESCALATION = 3

#: Hard ceiling on AI calls per day across the whole agent. When it is reached
#: everything falls back to templates and the nudges still go out — the cap
#: bounds spend, it never bounds delivery.
AI_DAILY_CALL_CAP = 500

#: Wall-clock budget for one copy call. A nudge is not worth waiting on.
AI_TIMEOUT_SECONDS = 8

_calls_today = {"date": None, "count": 0}


def should_use_ai(nudge_type: str, escalation: int = 0) -> bool:
    """Whether this specific nudge justifies an AI call.

    Pure apart from the daily counter — unit-testable.

    Args:
        nudge_type: e.g. "consecutive_miss".
        escalation: ladder level or stage.

    Returns:
        True only when AI copy is enabled, the type is allowlisted, the case is
        escalated enough to matter, and the daily cap has room.
    """
    if not getattr(settings, "enable_ai_copy", False):
        return False
    if nudge_type not in AI_ELIGIBLE_TYPES:
        return False
    if escalation < AI_MIN_ESCALATION:
        return False
    return _remaining_calls() > 0


def _remaining_calls() -> int:
    """Calls left in today's budget, resetting at midnight."""
    from app.utils.timezone import now_ist
    today = now_ist().date()
    if _calls_today["date"] != today:
        _calls_today["date"] = today
        _calls_today["count"] = 0
    return max(0, AI_DAILY_CALL_CAP - _calls_today["count"])


def _record_call() -> None:
    """Count one AI call against today's budget."""
    _remaining_calls()
    _calls_today["count"] += 1


def render(
    templates: Dict[Any, Any],
    key: Any,
    context: Dict[str, Any],
    nudge_type: str = "",
    escalation: int = 0,
    ai_writer: Optional[Callable[[str, Dict], Dict]] = None,
) -> Dict[str, str]:
    """Produce the copy for one nudge.

    Always renders the template first. That result is both the default and the
    fallback, so there is no path where a failed AI call leaves us with nothing.

    Args:
        templates: A template group from messages.py.
        key: Key within that group.
        context: Substitution values.
        nudge_type: Used to decide AI eligibility.
        escalation: Ladder level, also used for eligibility.
        ai_writer: Injected callable taking (template_result, context) and
            returning the same shape. Injected rather than imported so this
            module stays testable and provider-agnostic.

    Returns:
        {"title", "body", "severity", "cta", "template_id"} — `template_id`
        records which variant produced it, so copy performance is measurable
        instead of guessed at.
    """
    result = get_msg(templates, key, context)
    result["template_id"] = f"{nudge_type or 'generic'}:{key}"

    if not ai_writer or not should_use_ai(nudge_type, escalation):
        return result

    try:
        _record_call()
        written = ai_writer(result, context)
        if _usable(written):
            written.setdefault("severity", result["severity"])
            written.setdefault("cta", result["cta"])
            written["template_id"] = f"{nudge_type}:{key}:ai"
            return written
        log.warning("AI copy for %s was unusable — falling back to template", nudge_type)
    except Exception as exc:  # noqa: BLE001 — never let copy generation lose a nudge
        log.error("AI copy failed for %s (%s) — falling back to template",
                  nudge_type, exc)
    return result


def _usable(written: Optional[Dict]) -> bool:
    """Whether generated copy is safe to send.

    Cheap guards only. The expensive judgement lives in the prompt; this just
    refuses the obvious failures — empty output, or something far too long for
    a notification title.
    """
    if not isinstance(written, dict):
        return False
    title = (written.get("title") or "").strip()
    body = (written.get("body") or "").strip()
    if not title or not body:
        return False
    if len(title) > 90 or len(body) > 400:
        return False
    return True


def budget_status() -> Dict[str, Any]:
    """Today's AI copy usage, for the admin dashboard."""
    remaining = _remaining_calls()
    return {
        "enabled": bool(getattr(settings, "enable_ai_copy", False)),
        "cap": AI_DAILY_CALL_CAP,
        "used": _calls_today["count"],
        "remaining": remaining,
        "eligible_types": sorted(AI_ELIGIBLE_TYPES),
        "min_escalation": AI_MIN_ESCALATION,
    }
