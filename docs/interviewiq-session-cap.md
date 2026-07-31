# InterviewIQ — cap concurrent interviews (drop-in for the mock-test Space)

The request-level admission control (multiworker patch, Step 6) protects CPU.
This protects the expensive lane: each live interview continuously consumes
Sarvam STT/TTS and Claude calls for 10–15 minutes. The cap counts ACTIVE
INTERVIEWS and gates only `/session/start` — a student already mid-interview
is never refused a turn.

Rules encoded here:
- Full house → the start button gets the polite 503 the Vyom screen already
  renders: "All interview sessions are occupied just for a moment — a spot
  opens up shortly. Please try again in a few minutes."
- `/session/end` and `/session/abandon` free the slot immediately.
- A crashed browser cannot hold a slot forever: slots auto-expire after 15
  idle minutes.

## 1. Drop-in module — `interview_capacity.py`

```python
"""Active-interview cap. Gates session STARTS only — never mid-interview turns."""
import os
import threading
import time

CAP = int(os.environ.get("MAX_ACTIVE_INTERVIEWS", 50))
IDLE_TTL = int(os.environ.get("INTERVIEW_IDLE_TTL_SECONDS", 900))  # 15 min

BUSY_MESSAGE = (
    "All interview sessions are occupied just for a moment - a spot "
    "opens up shortly. Please try again in a few minutes."
)

_active: dict[str, float] = {}   # session_id -> last activity (epoch seconds)
_lock = threading.Lock()


def _sweep_locked(now: float) -> None:
    """Drop sessions idle past the TTL, so crashes never leak slots."""
    for sid, seen in list(_active.items()):
        if now - seen > IDLE_TTL:
            _active.pop(sid, None)


def try_admit(session_id: str) -> bool:
    """Claim a slot for a NEW interview. False = house full, send BUSY_MESSAGE."""
    now = time.time()
    with _lock:
        _sweep_locked(now)
        if len(_active) >= CAP:
            return False
        _active[session_id] = now
        return True


def touch(session_id: str) -> None:
    """Every turn calls this — keeps the slot alive during the interview."""
    with _lock:
        if session_id in _active:
            _active[session_id] = time.time()


def release(session_id: str) -> None:
    """End or abandon — slot free for the next student."""
    with _lock:
        _active.pop(session_id, None)


def active_count() -> int:
    """For your logs/monitoring."""
    with _lock:
        _sweep_locked(time.time())
        return len(_active)
```

## 2. Three call sites in the session routes

```python
from fastapi.responses import JSONResponse
import interview_capacity as capacity

# /session/start — AFTER validating the request, BEFORE the expensive setup:
    if not capacity.try_admit(session_id):
        return JSONResponse(
            status_code=503,
            content={"detail": capacity.BUSY_MESSAGE},
            headers={"Retry-After": "120"},
        )

# /session/turn — first line of the handler:
    capacity.touch(session_id)

# /session/end AND /session/abandon — after the session is closed:
    capacity.release(session_id)
```

The Vyom screen in the LMS already displays the 503's `detail` verbatim, and
the iframe app surfaces server messages the same way — no frontend change.

## 3. Choosing the cap

`MAX_ACTIVE_INTERVIEWS` is a Space secret/variable — change it without a
redeploy restart cost. Sizing it honestly:

- The real constraints are the Sarvam STT/TTS rate limits and your Anthropic
  tier — not CPU. Ask/check both limits, then set the cap comfortably below.
- 50 concurrent interviews ≈ 50 students speaking at once ≈ a 15-minute wave
  serves ~200 students/hour. For a 3–4k batch with the faculty stagger rule
  ("batch A this hour, batch B next"), 50 is a sound starting cap.
- Watch `active_count()` in the logs during the first assigned mock-test day
  and raise or lower from data, not guesses.

## 4. Multi-worker warning — read before combining with the workers patch

This module counts in ONE process. If the mock-test Space moves to multiple
uvicorn workers, either:
- keep sessions and this cap on 1 worker (fine if session state is already
  in-process memory — multiple workers would break sessions anyway), or
- move the count to the database the app already writes sessions to
  (`SELECT COUNT(*) ... WHERE status = 'active' AND last_activity > NOW() -
  INTERVAL 15 MINUTE`) so all workers see one number.

Check where the mock-test app keeps live session state BEFORE raising its
worker count — if it is in memory, workers must stay at 1 there regardless.
