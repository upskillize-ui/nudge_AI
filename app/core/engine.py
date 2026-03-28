"""
Hybrid Nudge Engine: Rules (80%) + AI/ML (20%)
Rules run FIRST on every event. AI runs ONLY for dropout prediction.
"""
import logging, os
from datetime import datetime, timedelta
from typing import Optional, List, Dict
from sqlalchemy.orm import Session
from sqlalchemy import func

from app.models import (Nudge, NudgeEvent, AttendanceTracker, RecordingTracker,
                         AssignmentTracker, TopicPerformance, StudentFeatures)
from app.core.messages import MISS, RECORDING, ASSIGNMENT, TOPIC, MENTOR, get_msg
from app.config import get_settings

log = logging.getLogger("engine")
settings = get_settings()

class NudgeEngine:
    def __init__(self, db: Session):
        self.db = db

    # ============ HELPERS ============
    def _create(self, user_id, role, ntype, title, body, severity="info",
                priority="medium", cta_text="", cta_url="", meta=None, escalation=0):
        if not self._can_send(user_id): return None
        if self.db.query(Nudge).filter(Nudge.user_id==user_id, Nudge.nudge_type==ntype,
            Nudge.created_at > datetime.utcnow()-timedelta(hours=4)).first():
            return None
        n = Nudge(user_id=user_id, user_role=role, nudge_type=ntype, priority=priority,
                  title=title, body=body, cta_text=cta_text, cta_url=cta_url,
                  severity=severity, metadata_json=meta or {}, status="pending",
                  scheduled_at=datetime.utcnow(), expires_at=datetime.utcnow()+timedelta(days=3),
                  escalation_level=escalation)
        self.db.add(n); self.db.commit(); self.db.refresh(n)
        log.info(f"Nudge: {ntype} -> {user_id} [{priority}]")
        return n

    def _can_send(self, uid):
        today = datetime.utcnow().replace(hour=0, minute=0, second=0)
        cnt = self.db.query(func.count(Nudge.id)).filter(
            Nudge.user_id==uid, Nudge.created_at>=today).scalar()
        return (cnt or 0) < settings.max_nudges_per_day

    # ============ RULE 1: LIVE ATTENDANCE ============
    def process_attendance(self, user_id, course_id, batch_id, attended,
                           lecture_title="", mentor_id="", student_name=""):
        t = self.db.query(AttendanceTracker).filter(
            AttendanceTracker.user_id==user_id, AttendanceTracker.course_id==course_id).first()
        if not t:
            t = AttendanceTracker(
                user_id=user_id, course_id=course_id, batch_id=batch_id,
                total_lectures=0, attended_count=0, consecutive_misses=0,
                max_consecutive=0, escalation_level=0
            )
            self.db.add(t)
            self.db.flush()

        # Safe defaults for any None values (existing rows with missing data)
        if t.total_lectures is None: t.total_lectures = 0
        if t.attended_count is None: t.attended_count = 0
        if t.consecutive_misses is None: t.consecutive_misses = 0
        if t.max_consecutive is None: t.max_consecutive = 0
        if t.escalation_level is None: t.escalation_level = 0

        t.total_lectures += 1
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
        ctx = {"name": name, "topic": lecture_title, "mentor": mentor_id or "your mentor", "pct": pct, "misses": m}

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
                RecordingTracker.user_id==sid, RecordingTracker.lecture_id==lecture_id).first():
                self.db.add(RecordingTracker(
                    user_id=sid, lecture_id=lecture_id, course_id=course_id,
                    batch_id=batch_id, lecture_title=title, recording_url=recording_url,
                    uploaded_at=up, expected_by=exp, watch_percent=0,
                    completed=False, reminder_count=0
                ))
        self.db.commit()

    def update_watch_progress(self, user_id, lecture_id, watch_percent):
        t = self.db.query(RecordingTracker).filter(
            RecordingTracker.user_id==user_id, RecordingTracker.lecture_id==lecture_id).first()
        if not t: return
        current = t.watch_percent or 0
        t.watch_percent = max(current, watch_percent)
        if not t.first_watched_at: t.first_watched_at = datetime.utcnow()
        t.last_watched_at = datetime.utcnow()
        t.completed = (t.watch_percent or 0) >= 80
        self.db.commit()

    def check_unwatched_recordings(self):
        """Cron job: find students who haven't watched recordings."""
        now = datetime.utcnow()
        nudges = []
        overdue = self.db.query(RecordingTracker).filter(
            RecordingTracker.completed==False, RecordingTracker.expected_by < now,
            RecordingTracker.expected_by > now - timedelta(days=14),
            RecordingTracker.reminder_count < 3).all()
        for t in overdue:
            if t.last_reminded_at and (now - t.last_reminded_at).total_seconds() < 24*3600: continue
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
        users_with_pending = self.db.query(RecordingTracker.user_id, RecordingTracker.batch_id,
            func.count(RecordingTracker.id).label("cnt")).filter(
            RecordingTracker.completed==False, RecordingTracker.expected_by < now
        ).group_by(RecordingTracker.user_id, RecordingTracker.batch_id).having(func.count(RecordingTracker.id) >= 3).all()
        batch_counts = {}
        for uid, bid, cnt in users_with_pending:
            batch_counts.setdefault(bid, 0)
            batch_counts[bid] += 1
        for bid, count in batch_counts.items():
            mm = get_msg(MENTOR, "recordings", {"count": count, "batch": bid})
            self._create(f"mentor_{bid}", "mentor", "mentor_alert", mm["title"], mm["body"],
                         "warning", "medium", mm["cta"], meta={"batch_id": bid, "type": "recordings_behind"})
        self.db.commit()
        return nudges

    # ============ RULE 3: ASSIGNMENTS ============
    def register_assignment(self, assignment_id, course_id, title, deadline, student_ids,
                            atype="assignment", closes=True):
        dl = datetime.fromisoformat(deadline) if isinstance(deadline, str) else deadline
        for sid in student_ids:
            if not self.db.query(AssignmentTracker).filter(
                AssignmentTracker.assignment_id==assignment_id, AssignmentTracker.user_id==sid).first():
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
            AssignmentTracker.assignment_id==assignment_id, AssignmentTracker.user_id==user_id).first()
        if t and not t.first_viewed_at: t.first_viewed_at = datetime.utcnow(); self.db.commit()

    def mark_submitted(self, assignment_id, user_id):
        t = self.db.query(AssignmentTracker).filter(
            AssignmentTracker.assignment_id==assignment_id, AssignmentTracker.user_id==user_id).first()
        if t: t.submitted_at = datetime.utcnow(); t.submission_status = "submitted"; self.db.commit()

    def check_deadlines(self):
        now = datetime.utcnow(); nudges = []
        for t in self.db.query(AssignmentTracker).filter(
            AssignmentTracker.submission_status != "submitted", AssignmentTracker.deadline > now).all():
            hrs = (t.deadline - now).total_seconds() / 3600
            ctx = {"title": t.title, "type": t.assignment_type, "days": (now-t.uploaded_at).days, "hours": round(hrs)}
            key = pri = None
            if hrs <= 6: key, pri = "6_hours", "critical"
            elif hrs <= 24: key, pri = "1_day", "critical"
            elif hrs <= 72: key, pri = "3_days", "high"
            elif not t.first_viewed_at and (now-t.uploaded_at).total_seconds() > 48*3600:
                key, pri = "not_viewed_48h", "medium"
            rc = t.reminder_count or 0
            if key and rc < 5:
                if t.last_reminded_at and (now-t.last_reminded_at).total_seconds() < 12*3600: continue
                msg = get_msg(ASSIGNMENT, key, ctx)
                n = self._create(t.user_id, "student", "assignment_deadline", msg["title"], msg["body"],
                                 msg["severity"], pri, msg["cta"], f"/assignments/{t.assignment_id}",
                                 {"assignment_id": t.assignment_id, "hours_left": round(hrs)})
                if n:
                    t.reminder_count = rc + 1
                    t.last_reminded_at = now
                    nudges.append(n)
        self.db.commit(); return nudges

    # ============ RULE 4: TOPIC PERFORMANCE ============
    def process_quiz(self, user_id, course_id, topic, score, batch_avg=None, name="", mentor_id=""):
        t = self.db.query(TopicPerformance).filter(TopicPerformance.user_id==user_id,
            TopicPerformance.course_id==course_id, TopicPerformance.topic_name==topic).first()
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
        if batch_avg: t.batch_average = batch_avg
        t.improvement_trend = "up" if old and score > old + 15 else ("down" if old and score < old - 10 else "flat")
        self.db.commit()

        nm = name or user_id
        ctx = {"name": nm, "topic": topic, "score": round(score), "avg": round(batch_avg or 0),
               "attempts": t.attempt_count, "old": round(old or 0)}
        if old and score >= old + 20:
            msg = get_msg(TOPIC, "improved", ctx)
            return self._create(user_id, "student", "topic_improvement", msg["title"], msg["body"], "success", "low", msg["cta"])
        if score < 50:
            if t.attempt_count >= 2 and all(s < 50 for s in scores[-2:]):
                msg = get_msg(TOPIC, "repeated", ctx)
                if mentor_id:
                    mm = get_msg(MENTOR, "low_scores", {"student": nm, "count": t.attempt_count})
                    self._create(mentor_id, "mentor", "mentor_alert", mm["title"], mm["body"], "warning", "high", mm["cta"])
                return self._create(user_id, "student", "topic_improvement", msg["title"], msg["body"], "warning", "high", msg["cta"])
            elif batch_avg and score < batch_avg - 30:
                msg = get_msg(TOPIC, "below_avg", ctx)
            else:
                msg = get_msg(TOPIC, "low", ctx)
            return self._create(user_id, "student", "topic_improvement", msg["title"], msg["body"], msg["severity"], "medium", msg["cta"])
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
            features = self.db.query(StudentFeatures).filter(StudentFeatures.dropped_out.is_(None)).all()
            if not features: return []
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
                        "prob": round(prob*100), "days": feat.days_since_last_login or 0})
                    n = self._create(f"mentor_{feat.course_id}", "mentor", "dropout_risk",
                        mm["title"], mm["body"], "critical", "critical", mm["cta"],
                        meta={"student_id": feat.user_id, "probability": round(prob, 3)})
                    if n: nudges.append(n)
            self.db.commit()
            log.info(f"Dropout prediction: {len(features)} students, {len(nudges)} high-risk alerts")
            return nudges
        except Exception as e:
            log.error(f"Dropout prediction failed: {e}")
            return []

    # ============ DASHBOARD QUERIES ============
    def get_critical_students(self, batch_id=""):
        results = []
        q = self.db.query(AttendanceTracker).filter(AttendanceTracker.consecutive_misses >= 3)
        if batch_id: q = q.filter(AttendanceTracker.batch_id == batch_id)
        for t in q.all():
            results.append({"user_id": t.user_id, "batch_id": t.batch_id, "type": "consecutive_miss",
                "risk": "critical" if (t.consecutive_misses or 0) >= 5 else "high",
                "misses": t.consecutive_misses or 0,
                "last_active": str(t.last_attended_at) if t.last_attended_at else None})
        now = datetime.utcnow()
        for t in self.db.query(AssignmentTracker).filter(AssignmentTracker.first_viewed_at.is_(None),
            AssignmentTracker.submission_status != "submitted", AssignmentTracker.deadline <= now + timedelta(days=3),
            AssignmentTracker.deadline > now).all():
            results.append({"user_id": t.user_id, "type": "assignment_unviewed", "risk": "high",
                "title": t.title, "hours_left": round((t.deadline-now).total_seconds()/3600)})
        for t in self.db.query(RecordingTracker).filter(RecordingTracker.completed==False,
            RecordingTracker.expected_by < now).all():
            results.append({"user_id": t.user_id, "type": "recording_unwatched", "risk": "medium",
                "lecture": t.lecture_title})
        results.sort(key=lambda x: {"critical":0,"high":1,"medium":2}.get(x.get("risk","medium"),3))
        return results

    def get_student_improvements(self, user_id):
        topics = self.db.query(TopicPerformance).filter(TopicPerformance.user_id==user_id).all()
        weak = [{"topic": t.topic_name, "score": t.latest_score or 0, "attempts": t.attempt_count or 0,
                 "avg": t.batch_average, "trend": t.improvement_trend} for t in topics if (t.latest_score or 0) < 50]
        strong = [{"topic": t.topic_name, "score": t.latest_score or 0, "attempts": t.attempt_count or 0,
                   "trend": t.improvement_trend} for t in topics if (t.latest_score or 0) >= 50]
        return {"weak": sorted(weak, key=lambda x: x["score"]), "strong": sorted(strong, key=lambda x: -x["score"])}

    def get_attendance_report(self, course_id="", batch_id=""):
        q = self.db.query(AttendanceTracker)
        if course_id: q = q.filter(AttendanceTracker.course_id == course_id)
        if batch_id: q = q.filter(AttendanceTracker.batch_id == batch_id)
        return [{"user_id": t.user_id, "course_id": t.course_id, "batch_id": t.batch_id,
                 "total": t.total_lectures or 0, "attended": t.attended_count or 0,
                 "pct": round(((t.attended_count or 0)/max(t.total_lectures or 1, 1))*100),
                 "consecutive_misses": t.consecutive_misses or 0,
                 "max_consecutive": t.max_consecutive or 0,
                 "last_attended": str(t.last_attended_at) if t.last_attended_at else None,
                 "status": "critical" if (t.consecutive_misses or 0)>=3 else ("warning" if (t.consecutive_misses or 0)>=2 else "ok")
                 } for t in q.all()]

    def get_recording_report(self, course_id="", batch_id=""):
        q = self.db.query(RecordingTracker)
        if course_id: q = q.filter(RecordingTracker.course_id == course_id)
        if batch_id: q = q.filter(RecordingTracker.batch_id == batch_id)
        return [{"user_id": t.user_id, "lecture_id": t.lecture_id, "title": t.lecture_title,
                 "watch_pct": t.watch_percent or 0, "completed": t.completed or False,
                 "uploaded_at": str(t.uploaded_at), "expected_by": str(t.expected_by),
                 "overdue": not t.completed and t.expected_by and t.expected_by < datetime.utcnow(),
                 "days_since_upload": (datetime.utcnow() - t.uploaded_at).days,
                 } for t in q.all()]