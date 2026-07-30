# NudgeAI — Pending Work and Bug Register

**As of 29 July 2026.** Ordered by what blocks the first real nudge.

Agent: v2.3.0 live and healthy, `db: true`, migration applied and verified.
LMS: commit `9af7417` pushed to GitHub — **not yet deployed to Render**.

---

## 0. Blocking right now

Nothing below matters until these three are done. Everything else in this
document is downstream of them.

| # | Item | Where | Symptom today |
|---|---|---|---|
| 0.1 | Deploy `9af7417` | Render → `upskillize-lms-backend` → Manual Deploy | Every new route returns `{"success":false,"message":"Route not found"}`. This is the 404 you saw on `admin/send`. |
| 0.2 | Set `NUDGE_API_KEY` and `NUDGE_WEBHOOK_SECRET` | Render → Environment | Once deployed, the 404 becomes a 401. Values must match the HuggingFace Space secrets exactly. |
| 0.3 | Push the agent `meta.type` fix | Agent@6 → GitHub + HF | Capstone, case studies and industry sessions all render under "Assignments" in the new panel. |

---

## 1. Broken or missing routes that stop NudgeAI working

These are the ones that interrupt the agent's actual job. The agent endpoint
exists and works; **nothing in the LMS calls it**, so the feature is dead.

| # | Agent endpoint | LMS caller | What breaks |
|---|---|---|---|
| 1.1 | `POST /webhook/class-scheduled` | **none** | Class reminders can never fire. `nudge_scheduled_classes` stays empty, so the 60/30/15-minute tiers have nothing to remind about. Source data already exists in `live_classes.scheduled_at`. |
| 1.2 | `POST /webhook/class-cancelled` | **none** | A cancelled class would still be reminded about. Only matters once 1.1 is wired — but wire them together. |
| 1.3 | `POST /webhook/activity-started` | **none** | Abandonment tracking never starts. |
| 1.4 | `POST /webhook/activity-progress` | **none** | No resume percentage, so "you are 40% through" copy has no data. |
| 1.5 | `POST /webhook/activity-completed` | **none** | Finished attempts are never closed, so a completed activity would still be chased. |
| 1.6 | `POST /webhook/contact` | **none** (see 1.7) | No contact rows. The agent's consent gate marks **every** email and WhatsApp message `skipped`. This is why email cannot work today. |

**Effect of 1.3–1.5 together:** Mock Interviews, Psychometric, Pathfinder,
Moonshot, Pulse Check and Profile are all fed by `activity_abandoned`. None of
them will ever produce a nudge until these three are wired. This is exactly
what you saw when you left an interview and a psychometric test half-finished
and nothing appeared — the agent was never told.

| # | Gap | Where | What breaks |
|---|---|---|---|
| 1.7 | `routes/nudge-contact-route.js` exists but I have not confirmed it is mounted in `server.js` or what it does | LMS | May already do part of 1.6, or may be dead code. **Check before building the contact sync** so we do not end up with two. |
| 1.8 | Case study publishing does not register coursework | `facultyProfile.js` ~line 2380 | Creates a `Notification` row but never calls `reportCourseworkPublished`. Case studies therefore get **no deadline reminders at all**, and the Case Studies category stays empty. |
| 1.9 | `adminAssessments.js` not wired | LMS | The `quizzes` table has **no `due_date` column**. Coursework registration is impossible without a schema change — this one needs a migration decision, not just code. |
| 1.10 | No outbox drainer | LMS | The agent queues email into `/outbox/email` and nothing ever collects it. Addressed by the files delivered with this document. |

---

## 2. Bugs and security

| # | Issue | Where | Severity |
|---|---|---|---|
| 2.1 | `PATCH /nudges/{id}/status` has no ownership check | Agent `routes/feed.py` | A logged-in user who guessed a nudge UUID could mark someone else's nudge read. Authenticated but not owner-verified. Fix belongs agent-side: accept `user_id` and reject mismatches. |
| 2.2 | Dashboard XSS | Agent `templates/dashboard.html` | Nudge title, body and user_id are interpolated unescaped. Also passes the API key in the query string, so it lands in server logs and browser history. |
| 2.3 | API key in the browser bundle | `NudgeAgent.jsx` | `VITE_NUDGE_API_KEY` is compiled into the frontend, so anyone can read it from the shipped JS and call the agent directly. Should be removed entirely — every call already proxies through the LMS. |
| 2.4 | `/health` stays green when a migration is missing | Agent `main.py` | It only runs `SELECT 1` and `COUNT(id)`, neither of which touches new columns. This nearly hid the missing `ALTER TABLE` today. **Verify schema changes with `SHOW COLUMNS`, never with the health endpoint.** |
| 2.5 | `datetime.utcnow()` deprecations | Agent, several services | 80 warnings on Python 3.12. Harmless on the 3.11 Space, will break on a future upgrade. |
| 2.6 | `UsageHistory.jsx` traps vertical scroll | LMS frontend | `.uh-page { height:100vh; overflow:hidden }` at ~L2011 and `.uh-grid { overflow:auto }` at ~L2129. Your own file — untouched by me. |
| 2.7 | Image bloat | Agent `requirements.txt` | xgboost, pandas and scikit-learn are pinned for the dropout model, which is disabled (`ai_enabled: false`). They are most of the image size and most of the cold-start time. |

---

## 3. Email — ready and waiting

**Status: built, dormant, safe.** The moment student email addresses exist in
the `users` table, one command turns it on.

### How it works

```
agent decides  ──queues──>  /outbox/email  ──drains──>  LMS mailer  ──> student
                                   ^                         |
                                   └────── marks sent ───────┘
```

The agent never talks to an SMTP server. It queues; the LMS sends, because the
LMS already owns the credentials and the sender reputation.

### Files delivered with this document

| File | Goes to | Purpose |
|---|---|---|
| `nudgeEmail.js` | `backend/services/` | Renders one nudge into branded HTML plus a plain-text fallback. Pure functions, no I/O. Table-based and inline-styled, because Outlook and most Indian webmail strips `<style>` blocks. Escapes every interpolated value and drops non-http CTA URLs. |
| `nudgeOutbox.js` | `backend/services/` | Contact sync and the outbox drainer. |

**No change to `nudgeAgent.js` is needed** — the generic `get`/`post` already
cover the new endpoints.

### Endpoints it uses

| Call | Direction | Purpose |
|---|---|---|
| `POST /api/v1/webhook/contact` | LMS → agent | Upsert address and consent flags |
| `GET /api/v1/outbox/email?limit=25` | LMS → agent | Collect queued messages |
| `POST /api/v1/outbox/email/{id}/sent?ok=true` | LMS → agent | Report the outcome so it is never sent twice |

### Turning it on, the day addresses land

```
# 1. one line in server.js, after the DB is ready
require('./services/nudgeOutbox').start();

# 2. in backend/.env (and Render)
SMTP_USER=...
SMTP_PASS=...
NUDGE_EMAIL_ENABLED=false     # leave false for the first run

# 3. push the addresses to the agent, once
node -e "require('./services/nudgeOutbox').syncContacts().then(console.log)"

# 4. dry run — logs what it WOULD send, sends nothing
node -e "require('./services/nudgeOutbox').drainEmail().then(console.log)"

# 5. when the dry-run log looks right
NUDGE_EMAIL_ENABLED=true
```

### Why it is safe to deploy before the addresses exist

Nothing sends. `syncContacts()` selects only rows that have an address, so with
none it writes nothing and logs *"no student email addresses yet"*. The agent's
consent gate then marks every queued email `skipped`, and `drainEmail()` finds
an empty outbox and returns. With `NUDGE_EMAIL_ENABLED=false` it will not send
even if everything else is in place — it logs each message and leaves it queued,
so flipping the flag later sends the backlog rather than losing it.

### The one thing to check

`nudgeOutbox.js` resolves your mailer by probing `../utils/mailer` for
`sendMail` / `sendEmail` / `send`. If your mailer lives elsewhere or exports a
different name, change `MAILER_PATH` and `CANDIDATES` at the top of the file —
nothing else needs touching. If it cannot find one it logs a clear warning and
pauses email rather than crashing the server.

### One decision still yours

`NUDGE_EMAIL_OPT_IN` defaults to `true`, on the basis that these are
course-related transactional messages to enrolled students, not marketing. If
your consent policy requires an explicit tick first, set it to `false` and
nothing is emailed until students opt in individually.

---

## 4. WhatsApp — not started

Six templates must be submitted to Meta as **UTILITY** (not marketing) and
approved before a single message can send. Approval takes one to two days, so
this is worth starting even though the code around it is not finished.

`class_starting_soon` · `deadline_final_hours` · `deadline_missed` ·
`activity_expiring` · `attendance_critical` · `certificate_ready`

The agent already refuses to queue WhatsApp for any nudge type without an
approved template, so an unapproved name cannot silently lose a message.

---

## 5. Remaining feature work

| # | Item | Notes |
|---|---|---|
| 5.1 | Swap rule services from `get_msg` to `copy.render` | Enables `template_id` attribution and the AI copy path. Own reviewable diff — do not mix with anything else. |
| 5.2 | Mentor cockpit | Faculty-side screens beyond the at-risk list. |
| 5.3 | `nudge_preferences` table | Documented in SETUP_GUIDE, never built. Per-student channel and quiet-hour overrides. |
| 5.4 | Streak data in `student/improvements` | The agent tracks streaks but the improvements payload does not expose them, so the panel cannot show a streak tile. |

---

## Working protocol — do not skip

`device_stage_files` reports the device's current byte count, but **the mount
does not refresh** once a path has been staged in a session. Trusting the
reported size overwrote two of the teammate's files (`attendanceLive.js`,
`student.js`); both were recovered with `git checkout`.

Before patching any live LMS file: copy it to a never-staged name
(`x.fresh.js`), stage that, assert `wc -c` matches the device-reported size,
and assert the base size inside the patch script.
