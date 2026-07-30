# InterviewIQ → NudgeAI integration (mock-test Space)

The LMS now reports interview activity from two places: the Vyom screen
(`/student/vyom`) reports start and completion directly, and every billed
InterviewIQ iteration sends a keep-alive heartbeat. The one gap left is the
**iframe app** at `/student/mock-interview` (served by the
`upskill25-mock-test` Space): its sessions start and end inside that Space,
so only that Space knows the exact moments. This doc is the 20-line change
that closes the gap.

## What to add, where

In the mock-test Space (the FastAPI app behind `/session/start`,
`/session/end`, `/session/abandon`), add one small module and three calls.

### 1. New Space secret

In the Space settings → Variables and secrets, add:

```
NUDGE_WEBHOOK_SECRET = <same value as NUDGE_WEBHOOK_SECRET on Render>
```

(Webhooks authenticate with the webhook secret, not the API key.) The agent
URL needs no secret — it is public: `https://upskill25-nudge-ai.hf.space`.

### 2. Drop-in module — `nudge_report.py`

```python
"""Fire-and-forget reports to the NudgeAI agent. Never blocks an interview."""
import os
import threading

import httpx

AGENT_URL = os.environ.get("NUDGE_AGENT_URL", "https://upskill25-nudge-ai.hf.space")
SECRET = os.environ.get("NUDGE_WEBHOOK_SECRET", "")


def _post(path: str, payload: dict) -> None:
    if not SECRET:
        return
    def run():
        try:
            httpx.post(
                f"{AGENT_URL}/api/v1/{path}",
                json=payload,
                headers={"X-Webhook-Secret": SECRET},
                timeout=6,
            )
        except Exception:
            pass  # reporting must never break an interview
    threading.Thread(target=run, daemon=True).start()


def interview_started(user_id: str, role: str = "") -> None:
    _post("webhook/activity-started", {
        "user_id": str(user_id),
        "activity_type": "interview",
        "activity_id": f"iq_{user_id}",
        "activity_name": f"Mock interview - {role}" if role else "Mock Interview",
        "resume_url": "/student/mock-interview",
    })


def interview_completed(user_id: str) -> None:
    _post("webhook/activity-completed", {
        "user_id": str(user_id),
        "activity_type": "interview",
        "activity_id": f"iq_{user_id}",
    })
```

### 3. Three call sites

```python
from nudge_report import interview_started, interview_completed

# in the /session/start handler, after the session is created:
interview_started(user_id, role=config.role)

# in the /session/end handler, after the report is generated:
interview_completed(user_id)

# in /session/abandon: add NOTHING. Leaving the attempt open is the point —
# the agent notices the silence and sends the follow-up nudge.
```

## Why the ids must not change

`activity_id` **must** be `iq_{user_id}` — the Vyom screen and the LMS
usage-ledger heartbeat both use that exact key. Same key means all three
sources update ONE attempt; a different key would create a second attempt
that nothing ever completes, and the student would get a false
"interview unfinished" nudge after every finished interview.

## What the student then experiences

Start an interview, finish it → attempt completed, no nudge.
Start an interview, walk away → after 30 idle minutes the honest follow-up:
"Your mock interview stopped midway — the debrief is where the learning
lives..." with escalation at 2 h and 24 h. No score-comparison, no shame,
one nudge per stage.
