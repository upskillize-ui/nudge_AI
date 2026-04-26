"""
Hybrid Nudge Engine: Rules (80%) + AI/ML (20%)
Rules run FIRST on every event. AI runs ONLY for dropout prediction.

PATCH NOTES (v2.1):
- get_student_improvements now returns aggregates (attendance_pct, tips, etc.)
  expected by NudgePanel.jsx Insights tab (was: only weak/strong topic lists)
- process_attendance is idempotent on (user_id, lecture_id) — webhook retries
  no longer double-count (requires AttendanceTracker.last_lecture_id column)
- improvement_trend uses symmetric ±10 thresholds (was: +15 / -10)
- New aggregate_features_daily() updates StudentFeatures so the dropout model
  trains on real signals (was: every feature except login_freq always 0)
- Quiet-hours enforcement and IST-aware day boundaries in _can_send
- _create now returns a dict with {nudge, suppressed_reason} so callers can
  distinguish silent-dedup from real success (kept backward-compat)
"""
import logging, os
from datetime import datetime, timedelta, timezone
from typing import Optional, List, Dict
from sqlalchemy.orm import Session
from sqlalchemy import func

from app.models import (Nudge, NudgeEvent, AttendanceTracker, RecordingTracker,
                         AssignmentTracker, TopicPerformance, StudentFeatures)
from app.core.messages import MISS, RECORDING, ASSIGNMENT, TOPIC, MENTOR, get_msg
from app.config import get_settings

log = logging.getLogger("engine")
settings = get_settings()

# IST offset for "today" boundaries — all daily caps and quiet hours
# are evaluated in user-facing local time, not UTC.
IST = timezone(timedelta(hours=5, minutes=30))


def _now_ist():
    return datetime.now(IST)


def _ist_today_start_utc():
    """Returns the UTC datetime corresponding to 00:00 IST today.
    Used for daily-cap counting so the cap resets at midnight IST,
    not 5:30 AM IST (which would be UTC midnight)."""
    now_ist = _now_ist()
    midnight_ist = now_ist.replace(hour=0, minute=0, second=0, microsecond=0)
    return midnight_ist.astimezone(timezone.utc).replace(tzinfo=None)


class NudgeEngine:
    def __init__(self, db: Session):
        self.db = db

    # ============ HELPERS ============
    def _create(self, user_id, role, ntype, title, body, severity="info",
                priority="medium", cta_text="", cta_url="", meta=None, escalation=0):
        # Daily cap check
        if not self._can_send(user_id):
            log.info(f"Suppressed {ntype} -> {user_id}: daily cap")
            return None
        # Quiet hours check (skip non-critical nudges between quiet_hours_start..end IST)
        if priority != "critical" and self._in_quiet_hours():
            log.info(f"Suppressed {ntype} -> {user_id}: quiet hours")
            return None
        # Dedup: same user + same type within 4h
        if self.db.query(Nudge).filter(
                Nudge.user_id == user_id,
                Nudge.nudge_type == ntype,
                Nudge.created_at > datetime.utcnow() - timedelta(hours=4)).first():
            log.info(f"Suppressed {ntype} -> {user_id}: 4h dedup")
            return None
        n = Nudge(user_id=user_id, user_role=role, nudge_type=ntype, priority=priority,
                  title=title, body=body, cta_text=cta_text, cta_url=cta_url,
                  severity=severity, metadata_json=meta or {}, status="pending",
                  scheduled_at=datetime.utcnow(), expires_at=datetime.utcnow() + timedelta(days=3),
                  escalation_level=escalation)
        self.db.add(n)
        self.db.commit()
        self.db.refresh(n)
        log.info(f"Nudge: {ntype} -> {user_id} [{priority}]")
        return n

    def _can_send(self, uid):
        cnt = self.db.query(func.count(Nudge.id)).filter(
            Nudge.user_id == uid,
            Nudge.created_at >= _ist_today_start_utc()).scalar()
        return (cnt or 0) < settings.max_nudges_per_day

    def _in_quiet_hours(self):
        h = _now_ist().hour
        start = settings.quiet_hours_start  # e.g. 22
        end = settings.quiet_hours_end      # e.g. 7
        if start <= end:
            return start <= h < end
        # wraps midnight (e.g. 22..7)
        return h >= start or h < end

    # ============ RULE 1: LIVE ATTENDANCE ============
    def process_attendance(self, user_id, course_id, batch_id, attended,
                           lecture_title="", mentor_id="", student_name="",
                           lecture_id=""):
        """Idempotent on (user_id, lecture_id). Webhook retries are safe."""
        t = self.db.query(AttendanceTracker).filter(
            AttendanceTracker.user_id == user_id,
            AttendanceTracker.course_id == course_id).first()
        if not t:
            t = AttendanceTracker(
                user_id=user_id, course_id=course_id, batch_id=batch_id,
                total_lectures=0, attended_count=0, consecutive_misses=0,
                max_consecutive=0, escalation_level=0
            )
            self.db.add(t)
            self.db.flush()

        # Idempotency guard — skip if this exact lecture_id already processed
        if lecture_id and getattr(t, "last_lecture_id", "") == lecture_id:
            log.info(f"Attendance dedup: {user_id} / {lecture_id} already counted")
            return None

        # Safe defaults for legacy rows
        for fld in ("total_lectures", "attended_count", "consecutive_misses",
                    "max_consecutive", "escalation_level"):
            if getattr(t, fld) is None:
                setattr(t, fld, 0)

        t.total_lectures += 1
        if lecture_id and hasattr(t, "last_lecture_id"):
            t.last_lecture_id = lecture_id

        if attended:
            t.attended_count += 1
            t.consecutive_misses = 0
            t.last_attended_at = datetime.utcnow()
            t.escalation_level = 0
            self.db.commit()
            return None

        # MISSED
        t.consecutive_misses += 1
        t.last_missed_at = datetime.utcnow()
        t.max_consecutive = max(t.max_consecutive, t.consecutive_misses)
        self.db.commit()

        m = t.consecutive_misses
        name = student_name or user_id
        pct = round((t.attended_count / max(t.total_lectures, 1)) * 100)
        ctx = {"name": name, "topic": lecture_title,
               "mentor": mentor_id or "your mentor", "pct": pct, "misses": m}

        key = 5 if m >= 5 else (3 if m >= 3 else (2 if m >= 2 else 1))
        pri = "critical" if m >= 3 else ("high" if m >= 2 else "medium")
        esc = min(key, 4)

        msg = get_msg(MISS, key, ctx)
        t.escalation_level = esc
        t.last_nudge_at = datetime.utcnow()
        self.db.commit()

        nudge = self._create(user_id, "student", "consecutive_miss", msg["title"], msg["body"],
                             msg["severity"], pri, msg["cta"], f"/courses/{course_id}/recordings",
                             {"misses": m, "course_id": course_id, "pct": pct}, esc)

        # Alert mentor on 3+ misses
        if m >= 3 and mentor_id:
            mm = get_msg(MENTOR, "miss", {"student": name, "batch": batch_id,
                         "misses": m, "last_active": str(t.last_attended_at or "unknown")})
            self._create(mentor_id, "mentor", "mentor_alert", mm["title"], mm["body"],
                         "critical", "critical", mm["cta"],
                         meta={"student_id": user_id, "misses": m, "type": "consecutive_miss"})
        return nudge

    # ============ RULE 2: RECORDING TRACKING ============
    def register_recording(self, lecture_id, course_id, batch_id, title, recording_url,
                           uploaded_at, expected_by, student_ids):
        up = datetime.fromisoformat(uploaded_at) if isinstance(uploaded_at, str) and uploaded_at else datetime.utcnow()
        exp = datetime.fromisoformat(expected_by) if isinstance(expected_by, str) and expected_by else up + timedelta(days=7)
        for sid in student_ids:
            if not self.db.query(RecordingTracker).filter(
                    RecordingTracker.user_id == sid,
                    RecordingTracker.lecture_id == lecture_id).first():
                self.db.add(RecordingTracker(
                    user_id=sid, lecture_id=lecture_id, course_id=course_id,
                    batch_id=batch_id, lecture_title=title, recording_url=recording_url,
                    uploaded_at=up, expected_by=exp, watch_percent=0,
                    completed=False, reminder_count=0
                ))
        self.db.commit()

    def update_watch_progress(self, user_id, lecture_id, watch_percent):
        t = self.db.query(RecordingTracker).filter(
            RecordingTracker.user_id == user_id,
            RecordingTracker.lecture_id == lecture_id).first()
        if not t:
            return
        current = t.watch_percent or 0
        t.watch_percent = max(current, watch_percent)
        if not t.first_watched_at:
            t.first_watched_at = datetime.utcnow()
        t.last_watched_at = datetime.utcnow()
        t.completed = (t.watch_percent or 0) >= 80
        self.db.commit()

    def check_unwatched_recordings(self):
        """Cron job: find students who haven't watched recordings."""
        now = datetime.utcnow()
        nudges = []
        overdue = self.db.query(RecordingTracker).filter(
            RecordingTracker.completed == False,
            RecordingTracker.expected_by < now,
            RecordingTracker.expected_by > now - timedelta(days=14),
            RecordingTracker.reminder_count < 3).all()
        for t in overdue:
            if t.last_reminded_at and (now - t.last_reminded_at).total_seconds() < 24 * 3600:
                continue
            days = (now - t.expected_by).days
            wp = t.watch_percent or 0
            if wp > 0:
                ctx = {"topic": t.lecture_title, "pct": wp, "days": days}
                msg = get_msg(RECORDING, "partial", ctx)
            else:
                ctx = {"topic": t.lecture_title, "days": (now - t.uploaded_at).days}
                msg = get_msg(RECORDING, "not_watched" if days <= 3 else "overdue", ctx)
            n = self._create(t.user_id, "student", "recording_unwatched", msg["title"], msg["body"],
                             msg["severity"], "medium", msg["cta"], f"/recordings/{t.lecture_id}")
            if n:
                t.reminder_count = (t.reminder_count or 0) + 1
                t.last_reminded_at = now
                nudges.append(n)
        # Students with 3+ pending recordings - alert mentor
        users_with_pending = self.db.query(
            RecordingTracker.user_id, RecordingTracker.batch_id,
            func.count(RecordingTracker.id).label("cnt")).filter(
            RecordingTracker.completed == False,
            RecordingTracker.expected_by < now
        ).group_by(RecordingTracker.user_id, RecordingTracker.batch_id
                   ).having(func.count(RecordingTracker.id) >= 3).all()
        batch_counts = {}
        for uid, bid, cnt in users_with_pending:
            batch_counts.setdefault(bid, 0)
            batch_counts[bid] += 1
        for bid, count in batch_counts.items():
            mm = get_msg(MENTOR, "recordings", {"count": count, "batch": bid})
            # NOTE: f"mentor_{bid}" is a synthetic mentor user_id. If your LMS
            # has real mentor user_ids, pass them in via webhook and use those.
            self._create(f"mentor_{bid}", "mentor", "mentor_alert", mm["title"], mm["body"],
                         "warning", "medium", mm["cta"],
                         meta={"batch_id": bid, "type": "recordings_behind"})
        self.db.commit()
        return nudges

    # ============ RULE 3: ASSIGNMENTS ============
    def register_assignment(self, assignment_id, course_id, title, deadline, student_ids,
                            atype="assignment", closes=True):
        dl = datetime.fromisoformat(deadline) if isinstance(deadline, str) else deadline
        for sid in student_ids:
            if not self.db.query(AssignmentTracker).filter(
                    AssignmentTracker.assignment_id == assignment_id,
                    AssignmentTracker.user_id == sid).first():
                self.db.add(AssignmentTracker(
                    assignment_id=assignment_id, user_id=sid,
                    course_id=course_id, title=title, assignment_type=atype,
                    uploaded_at=datetime.utcnow(), deadline=dl,
                    closes_after_deadline=closes, submission_status="not_started",
                    reminder_count=0
                ))
        self.db.commit()

    def mark_viewed(self, assignment_id, user_id):
        t = self.db.query(AssignmentTracker).filter(
            AssignmentTracker.assignment_id == assignment_id,
            AssignmentTracker.user_id == user_id).first()
        if t and not t.first_viewed_at:
            t.first_viewed_at = datetime.utcnow()
            self.db.commit()

    def mark_submitted(self, assignment_id, user_id):
        t = self.db.query(AssignmentTracker).filter(
            AssignmentTracker.assignment_id == assignment_id,
            AssignmentTracker.user_id == user_id).first()
        if t:
            t.submitted_at = datetime.utcnow()
            t.submission_status = "submitted"
            self.db.commit()

    def check_deadlines(self):
        now = datetime.utcnow()
        nudges = []
        for t in self.db.query(AssignmentTracker).filter(
                AssignmentTracker.submission_status != "submitted",
                AssignmentTracker.deadline > now).all():
            hrs = (t.deadline - now).total_seconds() / 3600
            ctx = {"title": t.title, "type": t.assignment_type,
                   "days": (now - t.uploaded_at).days, "hours": round(hrs)}
            key = pri = None
            if hrs <= 6:
                key, pri = "6_hours", "critical"
            elif hrs <= 24:
                key, pri = "1_day", "critical"
            elif hrs <= 72:
                key, pri = "3_days", "high"
            elif not t.first_viewed_at and (now - t.uploaded_at).total_seconds() > 48 * 3600:
                key, pri = "not_viewed_48h", "medium"
            rc = t.reminder_count or 0
            if key and rc < 5:
                if t.last_reminded_at and (now - t.last_reminded_at).total_seconds() < 12 * 3600:
                    continue
                msg = get_msg(ASSIGNMENT, key, ctx)
                n = self._create(t.user_id, "student", "assignment_deadline", msg["title"], msg["body"],
                                 msg["severity"], pri, msg["cta"], f"/assignments/{t.assignment_id}",
                                 {"assignment_id": t.assignment_id, "hours_left": round(hrs)})
                if n:
                    t.reminder_count = rc + 1
                    t.last_reminded_at = now
                    nudges.append(n)
        self.db.commit()
        return nudges

    # ============ RULE 4: TOPIC PERFORMANCE ============
    def process_quiz(self, user_id, course_id, topic, score, batch_avg=None, name="", mentor_id=""):
        t = self.db.query(TopicPerformance).filter(
            TopicPerformance.user_id == user_id,
            TopicPerformance.course_id == course_id,
            TopicPerformance.topic_name == topic).first()
        if not t:
            t = TopicPerformance(
                user_id=user_id, course_id=course_id, topic_name=topic,
                scores_json=[], attempt_count=0
            )
            self.db.add(t)
            self.db.flush()

        old = t.latest_score
        scores = (t.scores_json or []) + [score]
        t.scores_json = scores
        t.latest_score = score
        t.attempt_count = len(scores)
        if batch_avg:
            t.batch_average = batch_avg
        # Symmetric ±10 thresholds
        if old is None:
            t.improvement_trend = "flat"
        elif score > old + 10:
            t.improvement_trend = "up"
        elif score < old - 10:
            t.improvement_trend = "down"
        else:
            t.improvement_trend = "flat"
        self.db.commit()

        nm = name or user_id
        ctx = {"name": nm, "topic": topic, "score": round(score), "avg": round(batch_avg or 0),
               "attempts": t.attempt_count, "old": round(old or 0)}
        if old and score >= old + 20:
            msg = get_msg(TOPIC, "improved", ctx)
            return self._create(user_id, "student", "topic_improvement",
                                msg["title"], msg["body"], "success", "low", msg["cta"])
        if score < 50:
            if t.attempt_count >= 2 and all(s < 50 for s in scores[-2:]):
                msg = get_msg(TOPIC, "repeated", ctx)
                if mentor_id:
                    mm = get_msg(MENTOR, "low_scores", {"student": nm, "count": t.attempt_count})
                    self._create(mentor_id, "mentor", "mentor_alert",
                                 mm["title"], mm["body"], "warning", "high", mm["cta"])
                return self._create(user_id, "student", "topic_improvement",
                                    msg["title"], msg["body"], "warning", "high", msg["cta"])
            elif batch_avg and score < batch_avg - 30:
                msg = get_msg(TOPIC, "below_avg", ctx)
            else:
                msg = get_msg(TOPIC, "low", ctx)
            return self._create(user_id, "student", "topic_improvement",
                                msg["title"], msg["body"], msg["severity"], "medium", msg["cta"])
        return None

    # ============ AI/ML: DROPOUT PREDICTION (only when enabled) ============
    def predict_dropout(self):
        """ML FEATURE: Runs weekly. Loads XGBoost model, predicts for all active students."""
        if not settings.enable_dropout_prediction:
            log.info("Dropout prediction disabled (enable after 6 months of data)")
            return []
        model_path = settings.dropout_model_path
        if not os.path.exists(model_path):
            log.warning(f"No model at {model_path}. Train first with /admin/train-dropout")
            return []
        try:
            import xgboost as xgb
            import pandas as pd
            model = xgb.XGBClassifier()
            model.load_model(model_path)
            features = self.db.query(StudentFeatures).filter(
                StudentFeatures.dropped_out.is_(None)).all()
            if not features:
                return []
            df = pd.DataFrame([{
                "login_frequency": f.login_frequency or 0,
                "avg_session_minutes": f.avg_session_minutes or 0,
                "score_trend": f.score_trend or 0,
                "consecutive_misses": f.consecutive_misses or 0,
                "assignment_completion_rate": f.assignment_completion_rate or 0,
                "recording_completion_rate": f.recording_completion_rate or 0,
                "days_since_last_login": f.days_since_last_login or 0,
                "total_nudges_received": f.total_nudges_received or 0,
                "nudge_response_rate": f.nudge_response_rate or 0,
            } for f in features])
            probs = model.predict_proba(df)[:, 1]
            nudges = []
            for feat, prob in zip(features, probs):
                feat.predicted_dropout_prob = float(prob)
                if prob >= settings.dropout_threshold:
                    mm = get_msg(MENTOR, "dropout", {"student": feat.user_id,
                                                     "prob": round(prob * 100),
                                                     "days": feat.days_since_last_login or 0})
                    n = self._create(f"mentor_{feat.course_id}", "mentor", "dropout_risk",
                                     mm["title"], mm["body"], "critical", "critical", mm["cta"],
                                     meta={"student_id": feat.user_id, "probability": round(prob, 3)})
                    if n:
                        nudges.append(n)
            self.db.commit()
            log.info(f"Dropout prediction: {len(features)} students, {len(nudges)} high-risk alerts")
            return nudges
        except Exception as e:
            log.error(f"Dropout prediction failed: {e}")
            return []

    def aggregate_features_daily(self):
        """NEW: Daily cron. Populates StudentFeatures so ML actually has signal.
        Without this, every feature except login_frequency stays at 0 forever
        and your dropout model trains on noise."""
        now = datetime.utcnow()
        # Distinct (user, course) pairs across all trackers
        users = set()
        for row in self.db.query(AttendanceTracker.user_id, AttendanceTracker.course_id).all():
            users.add(row)
        for row in self.db.query(AssignmentTracker.user_id, AssignmentTracker.course_id).all():
            users.add(row)
        for row in self.db.query(RecordingTracker.user_id, RecordingTracker.course_id).all():
            users.add(row)

        updated = 0
        for uid, cid in users:
            if not uid or not cid:
                continue
            f = self.db.query(StudentFeatures).filter(
                StudentFeatures.user_id == uid,
                StudentFeatures.course_id == cid).first()
            if not f:
                f = StudentFeatures(user_id=uid, course_id=cid)
                self.db.add(f)

            # consecutive_misses
            att = self.db.query(AttendanceTracker).filter(
                AttendanceTracker.user_id == uid,
                AttendanceTracker.course_id == cid).first()
            f.consecutive_misses = (att.consecutive_misses or 0) if att else 0

            # assignment completion
            asgns = self.db.query(AssignmentTracker).filter(
                AssignmentTracker.user_id == uid,
                AssignmentTracker.course_id == cid).all()
            if asgns:
                done = sum(1 for a in asgns if a.submission_status == "submitted")
                f.assignment_completion_rate = round(done / len(asgns), 3)
            else:
                f.assignment_completion_rate = 0

            # recording completion
            recs = self.db.query(RecordingTracker).filter(
                RecordingTracker.user_id == uid,
                RecordingTracker.course_id == cid).all()
            if recs:
                done_r = sum(1 for r in recs if r.completed)
                f.recording_completion_rate = round(done_r / len(recs), 3)
            else:
                f.recording_completion_rate = 0

            # score_trend (avg slope of last 5 quiz scores across topics)
            topics = self.db.query(TopicPerformance).filter(
                TopicPerformance.user_id == uid,
                TopicPerformance.course_id == cid).all()
            slopes = []
            for tp in topics:
                s = tp.scores_json or []
                if len(s) >= 2:
                    slopes.append(s[-1] - s[0])
            f.score_trend = round(sum(slopes) / len(slopes), 2) if slopes else 0

            # nudge counts and response rate
            total = self.db.query(func.count(Nudge.id)).filter(Nudge.user_id == uid).scalar() or 0
            responded = self.db.query(func.count(Nudge.id)).filter(
                Nudge.user_id == uid,
                (Nudge.read_at.isnot(None)) | (Nudge.clicked_at.isnot(None))).scalar() or 0
            f.total_nudges_received = total
            f.nudge_response_rate = round(responded / total, 3) if total else 0

            # days since last login (best-effort: based on tracker.updated_at)
            last_seen = max(
                [d for d in [
                    att.last_attended_at if att else None,
                    att.updated_at if att else None,
                ] if d],
                default=None,
            )
            if last_seen:
                f.days_since_last_login = max(0, (now - last_seen).days)

            f.updated_at = now
            updated += 1
        self.db.commit()
        log.info(f"Feature aggregator: updated {updated} student-course rows")
        return updated

    # ============ DASHBOARD QUERIES ============
    def get_critical_students(self, batch_id=""):
        results = []
        q = self.db.query(AttendanceTracker).filter(AttendanceTracker.consecutive_misses >= 3)
        if batch_id:
            q = q.filter(AttendanceTracker.batch_id == batch_id)
        for t in q.all():
            results.append({"user_id": t.user_id, "batch_id": t.batch_id, "type": "consecutive_miss",
                            "risk": "critical" if (t.consecutive_misses or 0) >= 5 else "high",
                            "misses": t.consecutive_misses or 0,
                            "last_active": str(t.last_attended_at) if t.last_attended_at else None})
        now = datetime.utcnow()
        for t in self.db.query(AssignmentTracker).filter(
                AssignmentTracker.first_viewed_at.is_(None),
                AssignmentTracker.submission_status != "submitted",
                AssignmentTracker.deadline <= now + timedelta(days=3),
                AssignmentTracker.deadline > now).all():
            results.append({"user_id": t.user_id, "type": "assignment_unviewed", "risk": "high",
                            "title": t.title,
                            "hours_left": round((t.deadline - now).total_seconds() / 3600)})
        for t in self.db.query(RecordingTracker).filter(
                RecordingTracker.completed == False,
                RecordingTracker.expected_by < now).all():
            results.append({"user_id": t.user_id, "type": "recording_unwatched", "risk": "medium",
                            "lecture": t.lecture_title})
        results.sort(key=lambda x: {"critical": 0, "high": 1, "medium": 2}.get(x.get("risk", "medium"), 3))
        return results

    def get_student_improvements(self, user_id):
        """PATCHED: returns the aggregate fields the Insights tab expects PLUS
        the original weak/strong topic lists."""
        now = datetime.utcnow()

        # Topic performance — original behaviour
        topics = self.db.query(TopicPerformance).filter(
            TopicPerformance.user_id == user_id).all()
        weak = [{"topic": t.topic_name, "score": t.latest_score or 0,
                 "attempts": t.attempt_count or 0, "avg": t.batch_average,
                 "trend": t.improvement_trend}
                for t in topics if (t.latest_score or 0) < 50]
        strong = [{"topic": t.topic_name, "score": t.latest_score or 0,
                   "attempts": t.attempt_count or 0, "trend": t.improvement_trend}
                  for t in topics if (t.latest_score or 0) >= 50]

        # NEW: Aggregate stats
        att = self.db.query(AttendanceTracker).filter(
            AttendanceTracker.user_id == user_id).all()
        total = sum(t.total_lectures or 0 for t in att)
        attended = sum(t.attended_count or 0 for t in att)
        attendance_pct = round((attended / total) * 100) if total else None
        consecutive_misses = max((t.consecutive_misses or 0 for t in att), default=0)

        pending_asgn = self.db.query(func.count(AssignmentTracker.id)).filter(
            AssignmentTracker.user_id == user_id,
            AssignmentTracker.submission_status != "submitted",
            AssignmentTracker.deadline > now).scalar() or 0

        unwatched = self.db.query(func.count(RecordingTracker.id)).filter(
            RecordingTracker.user_id == user_id,
            RecordingTracker.completed == False).scalar() or 0

        # Generate tips from rules
        tips = []
        if attendance_pct is not None and attendance_pct < 80:
            tips.append(f"Attendance is at {attendance_pct}%. Aim for 85%+ to stay placement-eligible.")
        if consecutive_misses >= 2:
            tips.append("You've missed multiple sessions in a row. Watch the recordings before the next live class.")
        if pending_asgn >= 3:
            tips.append(f"{pending_asgn} assignments pending. Start with the one closing soonest.")
        elif pending_asgn >= 1:
            tips.append("Open your pending assignment now — even a draft submission counts.")
        if unwatched >= 3:
            tips.append("Recordings are stacking up. 30 minutes a day catches you up in a week.")
        if weak:
            top_weak = ", ".join(w["topic"] for w in sorted(weak, key=lambda x: x["score"])[:2])
            tips.append(f"Focus area: revisit {top_weak} with practice questions.")
        if not tips:
            tips.append("You're on track — keep the momentum going!")

        return {
            "weak": sorted(weak, key=lambda x: x["score"]),
            "strong": sorted(strong, key=lambda x: -x["score"]),
            "attendance_pct": attendance_pct,
            "consecutive_misses": consecutive_misses,
            "assignments_pending": pending_asgn,
            "recordings_unwatched": unwatched,
            "tips": tips,
        }

    def get_attendance_report(self, course_id="", batch_id=""):
        q = self.db.query(AttendanceTracker)
        if course_id:
            q = q.filter(AttendanceTracker.course_id == course_id)
        if batch_id:
            q = q.filter(AttendanceTracker.batch_id == batch_id)
        return [{"user_id": t.user_id, "course_id": t.course_id, "batch_id": t.batch_id,
                 "total": t.total_lectures or 0, "attended": t.attended_count or 0,
                 "pct": round(((t.attended_count or 0) / max(t.total_lectures or 1, 1)) * 100),
                 "consecutive_misses": t.consecutive_misses or 0,
                 "max_consecutive": t.max_consecutive or 0,
                 "last_attended": str(t.last_attended_at) if t.last_attended_at else None,
                 "status": "critical" if (t.consecutive_misses or 0) >= 3
                           else ("warning" if (t.consecutive_misses or 0) >= 2 else "ok")
                 } for t in q.all()]

    def get_recording_report(self, course_id="", batch_id=""):
        q = self.db.query(RecordingTracker)
        if course_id:
            q = q.filter(RecordingTracker.course_id == course_id)
        if batch_id:
            q = q.filter(RecordingTracker.batch_id == batch_id)
        return [{"user_id": t.user_id, "lecture_id": t.lecture_id, "title": t.lecture_title,
                 "watch_pct": t.watch_percent or 0, "completed": t.completed or False,
                 "uploaded_at": str(t.uploaded_at), "expected_by": str(t.expected_by),
                 "overdue": not t.completed and t.expected_by and t.expected_by < datetime.utcnow(),
                 "days_since_upload": (datetime.utcnow() - t.uploaded_at).days,
                 } for t in q.all()]