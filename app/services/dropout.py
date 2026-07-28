"""Dropout-risk feature aggregation and ML prediction.

The model is off by default. Until `enable_dropout_prediction` is set and a
trained model exists, only the nightly feature aggregation runs — which is
what makes the model trainable later.
"""
import logging
import os
from datetime import datetime
from typing import Dict, List, Optional, Set, Tuple

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import (AssignmentTracker, AttendanceTracker, Nudge,
                        RecordingTracker, StudentFeatures, TopicPerformance)
from app.services.messages import MENTOR, get_msg
from app.services.nudges import NudgeService

log = logging.getLogger("services.dropout")
settings = get_settings()

#: Feature columns, in the exact order the model expects them.
FEATURE_COLUMNS = [
    "login_frequency", "avg_session_minutes", "score_trend",
    "consecutive_misses", "assignment_completion_rate",
    "recording_completion_rate", "days_since_last_login",
    "total_nudges_received", "nudge_response_rate",
]


def _rate(numerator: int, denominator: int) -> float:
    """Safe ratio rounded to 3 places; 0 when there is nothing to divide."""
    return round(numerator / denominator, 3) if denominator else 0.0


class DropoutService:
    """Aggregates behavioural features and predicts dropout risk."""

    def __init__(self, db: Session):
        self.db = db
        self.nudges = NudgeService(db)

    # ---------- feature aggregation ----------

    def aggregate_features(self) -> int:
        """Nightly cron: refresh StudentFeatures from the tracker tables.

        Without this every feature except login_frequency stays at 0 and the
        model would train on noise.

        Returns:
            Number of (user, course) rows updated.
        """
        pairs = self._tracked_pairs()
        now = datetime.utcnow()
        updated = 0
        for user_id, course_id in pairs:
            if not user_id or not course_id:
                continue
            self._update_features(user_id, course_id, now)
            updated += 1
        self.db.commit()
        log.info("Feature aggregator: updated %d student-course rows", updated)
        return updated

    def _tracked_pairs(self) -> Set[Tuple[str, str]]:
        """Distinct (user_id, course_id) pairs across all tracker tables."""
        pairs: Set[Tuple[str, str]] = set()
        for model in (AttendanceTracker, AssignmentTracker, RecordingTracker):
            pairs.update(self.db.query(model.user_id, model.course_id).all())
        return pairs

    def _update_features(self, user_id: str, course_id: str, now: datetime) -> None:
        """Recompute one student's feature row for one course."""
        features = self.db.query(StudentFeatures).filter(
            StudentFeatures.user_id == user_id,
            StudentFeatures.course_id == course_id,
        ).first()
        if not features:
            features = StudentFeatures(user_id=user_id, course_id=course_id)
            self.db.add(features)

        attendance = self.db.query(AttendanceTracker).filter(
            AttendanceTracker.user_id == user_id,
            AttendanceTracker.course_id == course_id,
        ).first()
        features.consecutive_misses = (attendance.consecutive_misses or 0) if attendance else 0

        features.assignment_completion_rate = self._assignment_rate(user_id, course_id)
        features.recording_completion_rate = self._recording_rate(user_id, course_id)
        features.score_trend = self._score_trend(user_id, course_id)

        total, responded = self._nudge_engagement(user_id)
        features.total_nudges_received = total
        features.nudge_response_rate = _rate(responded, total)

        # Age the login gap from the real last login. Previously this was
        # derived from attendance activity, which overwrote the accurate value
        # the login webhook had set and made the feature meaningless.
        if features.last_login_at:
            features.days_since_last_login = max(0, (now - features.last_login_at).days)

        features.updated_at = now

    def _assignment_rate(self, user_id: str, course_id: str) -> float:
        """Fraction of this student's assignments that are submitted."""
        rows = self.db.query(AssignmentTracker.submission_status).filter(
            AssignmentTracker.user_id == user_id,
            AssignmentTracker.course_id == course_id,
        ).all()
        submitted = sum(1 for (status,) in rows if status == "submitted")
        return _rate(submitted, len(rows))

    def _recording_rate(self, user_id: str, course_id: str) -> float:
        """Fraction of this student's recordings watched to completion."""
        rows = self.db.query(RecordingTracker.completed).filter(
            RecordingTracker.user_id == user_id,
            RecordingTracker.course_id == course_id,
        ).all()
        done = sum(1 for (completed,) in rows if completed)
        return _rate(done, len(rows))

    def _score_trend(self, user_id: str, course_id: str) -> float:
        """Mean first-to-latest score movement across the student's topics."""
        topics = self.db.query(TopicPerformance.scores_json).filter(
            TopicPerformance.user_id == user_id,
            TopicPerformance.course_id == course_id,
        ).all()
        deltas = [
            scores[-1] - scores[0]
            for (scores,) in topics
            if scores and len(scores) >= 2
        ]
        return round(sum(deltas) / len(deltas), 2) if deltas else 0.0

    def _nudge_engagement(self, user_id: str) -> Tuple[int, int]:
        """Total nudges received and how many were read or clicked."""
        total = self.db.query(func.count(Nudge.id)).filter(
            Nudge.user_id == user_id,
        ).scalar() or 0
        responded = self.db.query(func.count(Nudge.id)).filter(
            Nudge.user_id == user_id,
            (Nudge.read_at.isnot(None)) | (Nudge.clicked_at.isnot(None)),
        ).scalar() or 0
        return total, responded

    # ---------- prediction ----------

    def predict(self) -> List[Nudge]:
        """Weekly cron: score unlabelled students and alert on high risk.

        Returns:
            Mentor alerts created, empty when prediction is disabled or no
            model is present.
        """
        if not settings.enable_dropout_prediction:
            log.info("Dropout prediction disabled")
            return []
        if not os.path.exists(settings.dropout_model_path):
            log.warning("No model at %s — train via /admin/train-dropout",
                        settings.dropout_model_path)
            return []

        try:
            return self._run_prediction()
        except Exception as exc:  # noqa: BLE001 — model/runtime failures are varied
            log.error("Dropout prediction failed: %s", exc)
            return []

    def _run_prediction(self) -> List[Nudge]:
        """Load the model, score every unlabelled student, alert on threshold."""
        import pandas as pd
        import xgboost as xgb

        rows = self.db.query(StudentFeatures).filter(
            StudentFeatures.dropped_out.is_(None),
        ).all()
        if not rows:
            return []

        model = xgb.XGBClassifier()
        model.load_model(settings.dropout_model_path)
        frame = pd.DataFrame([self.feature_vector(r) for r in rows])
        probabilities = model.predict_proba(frame)[:, 1]

        alerts = []
        for features, probability in zip(rows, probabilities):
            features.predicted_dropout_prob = float(probability)
            if probability >= settings.dropout_threshold:
                alert = self._alert(features, probability)
                if alert:
                    alerts.append(alert)
        self.db.commit()
        log.info("Dropout prediction: %d scored, %d high-risk", len(rows), len(alerts))
        return alerts

    @staticmethod
    def feature_vector(features: StudentFeatures) -> Dict[str, float]:
        """Extract the model's feature columns from a StudentFeatures row."""
        return {name: getattr(features, name) or 0 for name in FEATURE_COLUMNS}

    def _alert(self, features: StudentFeatures, probability: float) -> Optional[Nudge]:
        """Raise a high-risk alert for one student, if a mentor is known.

        Skips rather than writing to a synthesised recipient — an alert
        nobody reads is worse than no alert, because the dashboard counts it
        as delivered.
        """
        mentor_id = self._mentor_for_course(features.course_id)
        if not mentor_id:
            log.warning(
                "Student %s scored %.0f%% dropout risk but no mentor_id is known "
                "for course %s — alert skipped.",
                features.user_id, probability * 100, features.course_id,
            )
            return None

        message = get_msg(MENTOR, "dropout", {
            "student": features.user_id,
            "prob": round(probability * 100),
            "days": features.days_since_last_login or 0,
        })
        return self.nudges.create(
            user_id=mentor_id, role="mentor",
            nudge_type="dropout_risk", title=message["title"], body=message["body"],
            severity="critical", priority="critical", cta_text=message["cta"],
            meta={"student_id": features.user_id, "probability": round(probability, 3)},
        )

    def _mentor_for_course(self, course_id: str) -> str:
        """Most recently recorded mentor for a course, or "" if unknown."""
        row = self.db.query(RecordingTracker.mentor_id).filter(
            RecordingTracker.course_id == course_id,
            RecordingTracker.mentor_id != "",
        ).order_by(RecordingTracker.uploaded_at.desc()).first()
        return row[0] if row else ""
