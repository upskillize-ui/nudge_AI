import uuid
from datetime import datetime
from sqlalchemy import Column, String, Integer, Boolean, DateTime, Float, Text, JSON, Index
from app.database import Base

def uid(): return str(uuid.uuid4())

# ========== NUDGE STORAGE ==========
class Nudge(Base):
    __tablename__ = "nudge_nudges"
    id = Column(String(36), primary_key=True, default=uid)
    user_id = Column(String(100), nullable=False, index=True)
    user_role = Column(String(20), default="student")
    nudge_type = Column(String(40), nullable=False)
    priority = Column(String(10), default="medium")
    channel = Column(String(20), default="in_app")
    title = Column(String(300), nullable=False)
    body = Column(Text, nullable=False)
    cta_text = Column(String(100), default="")
    cta_url = Column(String(500), default="")
    severity = Column(String(15), default="info")
    metadata_json = Column(JSON, default=dict)
    status = Column(String(15), default="pending")
    scheduled_at = Column(DateTime, default=datetime.utcnow)
    delivered_at = Column(DateTime, nullable=True)
    read_at = Column(DateTime, nullable=True)
    clicked_at = Column(DateTime, nullable=True)
    dismissed_at = Column(DateTime, nullable=True)
    expires_at = Column(DateTime, nullable=True)
    escalation_level = Column(Integer, default=0)
    created_at = Column(DateTime, default=datetime.utcnow)
    __table_args__ = (
        Index("idx_n_user_status", "user_id", "status"),
        Index("idx_n_type", "nudge_type", "status"),
        Index("idx_n_sched", "scheduled_at"),
    )

class NudgeEvent(Base):
    __tablename__ = "nudge_events"
    id = Column(Integer, primary_key=True, autoincrement=True)
    nudge_id = Column(String(36), nullable=False, index=True)
    user_id = Column(String(100), nullable=False)
    event_type = Column(String(20), nullable=False)
    channel = Column(String(20), default="in_app")
    created_at = Column(DateTime, default=datetime.utcnow)

# ========== ATTENDANCE TRACKING ==========
class AttendanceTracker(Base):
    """Tracks consecutive misses for LIVE lectures."""
    __tablename__ = "nudge_attendance"
    id = Column(String(36), primary_key=True, default=uid)
    user_id = Column(String(100), nullable=False)
    course_id = Column(String(100), nullable=False)
    batch_id = Column(String(100), default="")
    total_lectures = Column(Integer, default=0)
    attended_count = Column(Integer, default=0)
    consecutive_misses = Column(Integer, default=0)
    max_consecutive = Column(Integer, default=0)
    last_attended_at = Column(DateTime, nullable=True)
    last_missed_at = Column(DateTime, nullable=True)
    escalation_level = Column(Integer, default=0)
    last_nudge_at = Column(DateTime, nullable=True)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    __table_args__ = (Index("idx_att_uc", "user_id", "course_id", unique=True),)

class RecordingTracker(Base):
    """Tracks who watched recorded lectures and who didn't."""
    __tablename__ = "nudge_recordings"
    id = Column(String(36), primary_key=True, default=uid)
    user_id = Column(String(100), nullable=False)
    lecture_id = Column(String(100), nullable=False)
    course_id = Column(String(100), nullable=False)
    batch_id = Column(String(100), default="")
    lecture_title = Column(String(300), default="")
    recording_url = Column(String(500), default="")
    uploaded_at = Column(DateTime, nullable=False)
    expected_by = Column(DateTime, nullable=True)  # should watch by this date
    watch_percent = Column(Integer, default=0)  # 0-100
    completed = Column(Boolean, default=False)  # True if watch_percent >= 80
    first_watched_at = Column(DateTime, nullable=True)
    last_watched_at = Column(DateTime, nullable=True)
    reminder_count = Column(Integer, default=0)
    last_reminded_at = Column(DateTime, nullable=True)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    __table_args__ = (
        Index("idx_rec_ul", "user_id", "lecture_id", unique=True),
        Index("idx_rec_unwatched", "completed", "expected_by"),
    )

# ========== ASSIGNMENT TRACKING ==========
class AssignmentTracker(Base):
    __tablename__ = "nudge_assignments"
    id = Column(String(36), primary_key=True, default=uid)
    assignment_id = Column(String(100), nullable=False)
    user_id = Column(String(100), nullable=False)
    course_id = Column(String(100), default="")
    title = Column(String(300), default="")
    assignment_type = Column(String(30), default="assignment")
    uploaded_at = Column(DateTime, nullable=False)
    deadline = Column(DateTime, nullable=False)
    first_viewed_at = Column(DateTime, nullable=True)
    submitted_at = Column(DateTime, nullable=True)
    submission_status = Column(String(20), default="not_started")
    closes_after_deadline = Column(Boolean, default=True)
    score = Column(Float, nullable=True)
    mentor_feedback = Column(JSON, nullable=True)
    reminder_count = Column(Integer, default=0)
    last_reminded_at = Column(DateTime, nullable=True)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    __table_args__ = (Index("idx_asgn_ua", "user_id", "assignment_id", unique=True),)

# ========== TOPIC PERFORMANCE ==========
class TopicPerformance(Base):
    __tablename__ = "nudge_topics"
    id = Column(String(36), primary_key=True, default=uid)
    user_id = Column(String(100), nullable=False)
    course_id = Column(String(100), nullable=False)
    topic_name = Column(String(200), nullable=False)
    scores_json = Column(JSON, default=list)
    latest_score = Column(Float, nullable=True)
    attempt_count = Column(Integer, default=0)
    batch_average = Column(Float, nullable=True)
    improvement_trend = Column(String(10), default="flat")
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    __table_args__ = (Index("idx_tp_uct", "user_id", "course_id", "topic_name", unique=True),)

# ========== DROPOUT FEATURES (for ML training) ==========
class StudentFeatures(Base):
    """Aggregated features for ML dropout prediction. Updated daily by cron."""
    __tablename__ = "nudge_student_features"
    user_id = Column(String(100), primary_key=True)
    course_id = Column(String(100), primary_key=True)
    login_frequency = Column(Float, default=0)  # logins per week
    avg_session_minutes = Column(Float, default=0)
    score_trend = Column(Float, default=0)  # positive = improving
    consecutive_misses = Column(Integer, default=0)
    assignment_completion_rate = Column(Float, default=0)
    recording_completion_rate = Column(Float, default=0)
    days_since_last_login = Column(Integer, default=0)
    total_nudges_received = Column(Integer, default=0)
    nudge_response_rate = Column(Float, default=0)
    dropped_out = Column(Boolean, nullable=True)  # NULL = still active, True/False = labeled
    predicted_dropout_prob = Column(Float, nullable=True)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
