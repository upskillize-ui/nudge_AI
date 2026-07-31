# Multi-worker patch for every Upskillize agent Space

NudgeAI served one request at a time for months — one uvicorn worker on the
free 2-vCPU Space, so every student queued behind the previous one. The fix
shipped in NudgeAI (main.py + Dockerfile) applies almost verbatim to the
other agents. This doc is the recipe, per Space.

**Why one worker was ever the setting:** a Space that runs background jobs
(APScheduler, a sweep loop) duplicates those jobs once per worker — NudgeAI
with 2 workers once sent every nudge twice. The cure is NOT one worker; it is
**leader election**: all workers serve HTTP, exactly one runs the jobs.

---

## Step 1 — check whether the agent has background jobs

In the agent's repo:

```
findstr /s /i "APScheduler BackgroundScheduler start_scheduler create_task while True" main.py app\*.py
```

- **No scheduler found** → only Step 2 is needed (one line).
- **Scheduler found** → Step 2 + Step 3.

## Step 2 — Dockerfile: run 4 workers

Change the CMD's `--workers` value (add it if absent):

```dockerfile
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "7860", "--workers", "4"]
```

4 workers fits the free 2-vCPU/16GB tier for these agents (they are I/O-bound:
waiting on Anthropic/DB, not crunching CPU). If the agent keeps large models
in memory per process, drop to 2.

## Step 3 — leader election (only for agents WITH background jobs)

Paste into `main.py` above the lifespan/startup function:

```python
def _elect_scheduler_leader(port: int = 7899) -> bool:
    """Exactly one worker runs the background jobs.

    Every uvicorn worker imports this module; if each started the scheduler,
    every cron tick would fire N times (NudgeAI once double-sent every nudge
    this way). The worker that wins a localhost port bind is the leader; the
    rest only serve HTTP. No files, no dependencies, survives any container.
    """
    import socket

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind(("127.0.0.1", port))
        sock.listen(1)
        _elect_scheduler_leader._lock = sock  # keep alive for process lifetime
        return True
    except OSError:
        sock.close()
        return False
```

Then wrap the existing scheduler start:

```python
# BEFORE
start_scheduler()

# AFTER
if _elect_scheduler_leader():
    start_scheduler()
    log.info("This worker is the scheduler leader")
else:
    log.info("Scheduler runs in another worker — this one only serves HTTP")
```

## Step 4 — check the DB pool per worker

4 workers × pool size = connections held against Aiven. If the agent creates
a SQLAlchemy engine or PyMySQL pool, keep `pool_size` ≤ 3 per worker
(12 total per Space). NudgeAI's default is fine; check agents that set a
larger pool.

## Step 5 — deploy and verify

```
git add -A
git commit -m "4 workers with scheduler leader election"
git push origin main
git push hf main
```

After the Space rebuilds, the container log must show the startup banner
**four times**, and (for scheduler agents) exactly **one** line saying it is
the scheduler leader. If you see the leader line more than once, stop and
report — that would mean duplicate jobs.

## Where to apply

| Space | Agent | Scheduler expected? |
|---|---|---|
| upskill25/Nudge_AI | NudgeAI | DONE — reference implementation |
| upskill25/mock-test | InterviewIQ backend | check (session cleanup loops) |
| upskill25/airev-agent | AiRev | check |
| TestGen Space | TestGen / BrainDrill | check |
| upskill25/ai-enhancer | ProfileIQ | check |

Voice-heavy InterviewIQ benefits most — a 10-minute interview no longer
blocks every other student's request.

## When code is not enough

Workers fix *queuing*. If an agent is still slow after this, the work itself
is slow (AI calls, shared CPU) — upgrade that Space's hardware:
Space page → Settings → Space hardware → CPU Upgrade (8 vCPU, ~$0.03/hr ≈
$22/month). Note: the $9 PRO account subscription does NOT change Space
hardware. Upgrade only the Spaces students actually hammer.

---

## Step 6 — admission control (do this together with the workers)

When every slot is genuinely busy (an all-batch mock test), a new request
must get an honest answer, not an invisible queue ending in a timeout.
NudgeAI's reference implementation is `app/utils/admission.py` in the
Nudge_AI repo — copy that file into each agent, then:

```python
# main.py (or wherever the FastAPI app is created)
from app.utils.admission import AdmissionControl
from app.config import get_settings  # or read the env var directly

app = create_app()          # FastAPI instance — routes attach to this
asgi = AdmissionControl(app, int(os.environ.get("MAX_CONCURRENT_REQUESTS", 25)))
```

```dockerfile
CMD ["uvicorn", "main:asgi", "--host", "0.0.0.0", "--port", "7860", "--workers", "4"]
```

IMPORTANT: wrap AFTER all `@app.get/...` routes are registered (export a
separate `asgi` object; keep decorating `app`). Serving `main:app` instead of
`main:asgi` silently disables the protection.

Caps per worker, by agent type:
- Fast DB agents (NudgeAI): 80 (already set)
- AI/chat agents (AiRev, TestGen, ProfileIQ): 25
- Voice interviews (InterviewIQ backend): 15 — each session is heavy

Students see: "All sessions are occupied just for a moment — a spot opens
up shortly. Please try again in a few minutes." (Positive, certain of
return, no internal load details, and no claims about saved work — just the
wait and the way back.) (HTTP 503 + Retry-After: 60; the Vyom frontend already renders it.)
/health stays exempt so keep-warm never gets refused.
