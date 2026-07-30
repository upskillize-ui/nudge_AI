"""Topic-level quiz performance and coaching nudges."""
import logging
from datetime import datetime
from typing import Dict, List, Optional

from sqlalchemy.orm import Session

from app.models import Nudge, TopicPerformance
from app.services.copy import render
from app.services.messages import MENTOR, SCORE, TOPIC
from app.services.nudges import NudgeService

log = logging.getLogger("services.topics")

#: Score bands, most demanding first. The first band whose `min_score` is met
#: wins. Note the deliberate gap: 50-84 has NO band, because a student who
#: scored 71 does not need a notification about it. Silence is a feature —
#: nudging every result is how the bell becomes noise.
SCORE_BANDS = [
    {"min_score": 95, "key": "exceptional", "severity": "success",
     "priority": "low", "nudge_type": "score_exceptional", "alert_mentor": False},
    {"min_score": 85, "key": "strong", "severity": "success",
     "priority": "low", "nudge_type": "score_strong", "alert_mentor": False},
    {"min_score": 35, "key": "low", "severity": "warning",
     "priority": "medium", "nudge_type": "topic_improvement", "alert_mentor": False},
    {"min_score": 0, "key": "repeated", "severity": "warning",
     "priority": "high", "nudge_type": "score_critical", "alert_mentor": True},
]

#: Scores inside this range send nothing at all.
SILENT_BAND = (50, 84)

#: Score at or above which a topic counts as understood.
PASS_MARK = 50

#: Symmetric band, in points, inside which a score change reads as "flat".
TREND_BAND = 10

#: Improvement, in points, that earns a celebration nudge.
CELEBRATION_DELTA = 20

#: How far below the batch average counts as a gap worth naming.
BELOW_AVERAGE_GAP = 30

#: Consecutive sub-pass attempts before the mentor is looped in.
REPEATED_FAILURE_ATTEMPTS = 2


def trend_for(previous: Optional[float], current: float) -> str:
    """Classify a score movement as up, down or flat.

    Pure function — unit-testable without a database.

    Args:
        previous: Prior latest score, or None on first attempt.
        current: The new score.

    Returns:
        "up", "down" or "flat".
    """
    if previous is None:
        return "flat"
    if current > previous + TREND_BAND:
        return "up"
    if current < previous - TREND_BAND:
        return "down"
    return "flat"


def band_for(score: float) -> Optional[dict]:
    """The score band a result falls into, or None when it should be silent.

    Pure function — unit-testable without a database.

    Args:
        score: percentage, 0-100.

    Returns:
        The band dict, or None for anything in the silent 50-84 range.
    """
    if SILENT_BAND[0] <= score <= SILENT_BAND[1]:
        return None
    for band in SCORE_BANDS:
        if score >= band["min_score"]:
            return band
    return None


class TopicService:
    """Records quiz scores and coaches on weak topics."""

    def __init__(self, db: Session):
        self.db = db
        self.nudges = NudgeService(db)

    def process_quiz(
        self,
        user_id: str,
        course_id: str,
        topic_name: str,
        score: float,
        batch_average: Optional[float] = None,
        student_name: str = "",
        mentor_id: str = "",
    ) -> Optional[Nudge]:
        """Record a score and send the matching coaching or praise nudge.

        Returns:
            The student nudge if one was created, else None.
        """
        performance = self._get_or_create(user_id, course_id, topic_name)
        previous = performance.latest_score
        self._apply_score(performance, score, batch_average)

        context = {
            "name": student_name or user_id, "topic": topic_name,
            "score": round(score), "avg": round(batch_average or 0),
            "attempts": performance.attempt_count, "old": round(previous or 0),
        }

        # A comeback outranks the band: 31 -> 58 is progress, not "needs work".
        if previous is not None and score >= previous + CELEBRATION_DELTA:
            return self._celebrate(user_id, context)

        band = band_for(score)
        if not band:
            return None      # 50-84: deliberately silent

        if band["key"] in ("exceptional", "strong"):
            return self._praise(user_id, band, context, score, batch_average)

        return self._coach(performance, user_id, context, batch_average,
                           score, mentor_id, student_name or user_id)

    def _get_or_create(
        self, user_id: str, course_id: str, topic_name: str
    ) -> TopicPerformance:
        """Fetch the topic row for this student, creating it if absent."""
        performance = self.db.query(TopicPerformance).filter(
            TopicPerformance.user_id == user_id,
            TopicPerformance.course_id == course_id,
            TopicPerformance.topic_name == topic_name,
        ).first()
        if performance:
            return performance
        performance = TopicPerformance(
            user_id=user_id, course_id=course_id, topic_name=topic_name,
            scores_json=[], attempt_count=0,
        )
        self.db.add(performance)
        self.db.flush()
        return performance

    def _apply_score(
        self, performance: TopicPerformance, score: float,
        batch_average: Optional[float],
    ) -> None:
        """Append the score and refresh the derived trend fields."""
        previous = performance.latest_score
        scores = (performance.scores_json or []) + [score]
        performance.scores_json = scores
        performance.latest_score = score
        performance.attempt_count = len(scores)
        if batch_average:
            performance.batch_average = batch_average
        performance.improvement_trend = trend_for(previous, score)
        self.db.commit()

    def _celebrate(self, user_id: str, context: Dict) -> Optional[Nudge]:
        """Send a comeback nudge for a large improvement."""
        message = render(TOPIC, "improved", context, nudge_type="topic_improvement")
        return self.nudges.create(
            user_id=user_id, role="student", nudge_type="topic_improvement",
            title=message["title"], body=message["body"],
            template_id=message["template_id"],
            severity="success", priority="low", cta_text=message["cta"],
        )

    def _praise(
        self, user_id: str, band: Dict, context: Dict,
        score: float, batch_average: Optional[float],
    ) -> Optional[Nudge]:
        """Recognise a high score by naming the number, not the person."""
        delta = round(score - batch_average) if batch_average else 0
        message = render(SCORE, band["key"], {**context, "delta": delta},
                         nudge_type=band["nudge_type"],
                         escalation=3 if band["alert_mentor"] else 0)
        return self.nudges.create(
            user_id=user_id, role="student", nudge_type=band["nudge_type"],
            title=message["title"], body=message["body"],
            template_id=message["template_id"],
            severity=band["severity"], priority=band["priority"],
            cta_text=message["cta"],
            meta={"score": round(score), "topic": context.get("topic", ""),
                  "band": band["key"]},
        )

    def _coach(
        self,
        performance: TopicPerformance,
        user_id: str,
        context: Dict,
        batch_average: Optional[float],
        score: float,
        mentor_id: str,
        student_name: str,
    ) -> Optional[Nudge]:
        """Send the appropriate low-score nudge, looping in the mentor if needed."""
        recent = (performance.scores_json or [])[-REPEATED_FAILURE_ATTEMPTS:]
        repeated = (
            performance.attempt_count >= REPEATED_FAILURE_ATTEMPTS
            and len(recent) == REPEATED_FAILURE_ATTEMPTS
            and all(s < PASS_MARK for s in recent)
        )

        if repeated:
            if mentor_id:
                self._alert_mentor(mentor_id, student_name, performance.attempt_count)
            message = render(TOPIC, "repeated", context, nudge_type="topic_improvement")
            return self.nudges.create(
                user_id=user_id, role="student", nudge_type="topic_improvement",
                title=message["title"], body=message["body"],
                template_id=message["template_id"],
                severity="warning", priority="high", cta_text=message["cta"],
            )

        if batch_average and score < batch_average - BELOW_AVERAGE_GAP:
            key = "below_avg"
        else:
            key = "low"
        message = render(TOPIC, key, context, nudge_type="topic_improvement")
        return self.nudges.create(
            user_id=user_id, role="student", nudge_type="topic_improvement",
            title=message["title"], body=message["body"],
            template_id=message["template_id"],
            severity=message["severity"], priority="medium", cta_text=message["cta"],
        )

    def _alert_mentor(self, mentor_id: str, student_name: str, attempts: int) -> None:
        """Tell the mentor a student is repeatedly scoring below the pass mark."""
        message = render(MENTOR, "low_scores", {"student": student_name, "count": attempts},
                         nudge_type="mentor_alert", escalation=attempts)
        self.nudges.create(
            user_id=mentor_id, role="mentor", nudge_type="mentor_alert",
            title=message["title"], body=message["body"],
            template_id=message["template_id"],
            severity="warning", priority="high", cta_text=message["cta"],
        )

    def for_student(self, user_id: str) -> Dict[str, List[Dict]]:
        """Split a student's topics into weak and strong lists."""
        topics = self.db.query(TopicPerformance).filter(
            TopicPerformance.user_id == user_id,
        ).all()
        weak, strong = [], []
        for topic in topics:
            row = {
                "topic": topic.topic_name, "score": topic.latest_score or 0,
                "attempts": topic.attempt_count or 0, "trend": topic.improvement_trend,
            }
            if (topic.latest_score or 0) < PASS_MARK:
                weak.append({**row, "avg": topic.batch_average})
            else:
                strong.append(row)
        return {
            "weak": sorted(weak, key=lambda r: r["score"]),
            "strong": sorted(strong, key=lambda r: -r["score"]),
        }
