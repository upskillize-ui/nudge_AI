"""
PATCH NOTES (v2.2):
- RecordingTracker.mentor_id — the real mentor for the batch, so overdue-
  recording alerts reach a person instead of a synthesised "mentor_{batch}".
- StudentFeatures.last_login_at — the actual last login. days_since_last_login
  is now derived from this rather than being overwritten nightly from
  attendance activity.

PATCH NOTES (v2.1):
- AttendanceTracker.last_lecture_id — idempotent webhook retries.

REQUIRED MIGRATION (run once on Aiven MySQL, see sql/002_v22.sql):
    ALTER TABLE nudge_attendance
      ADD COLUMN last_lecture_id VARCHAR(100) DEFAULT '';
    ALTER TABLE nudge_recordings
      ADD COLUMN mentor_id VARCHAR(100) DEFAULT '';
    ALTER TABLE nudge_student_features
      ADD COLUMN last_login_at DATETIME NULL;

On a fresh DB init_db() creates these automatically. On an existing DB run
the ALTERs first.
"""
import uuid
from datetime import datetime
from sqlalchemy import Column, String, Integer, Boolean, DateTime, Float, Text, JSON, Index
from app.database import Base


def uid():
    return str(uuid.uuid4())


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
    metadata_json = Column(JSON, default=lambda: {})
    status = Column(String(15), default="pending")
    scheduled_at = Column(DateTime, default=datetime.utcnow)
    delivered_at = Column(DateTime, nullable=True)
    read_at = Column(DateTime, nullable=True)
    clicked_at = Column(DateTime, nullable=True)
    dismissed_at = Column(DateTime, nullable=True)
    expires_at = Column(DateTime, nullable=True)
    escalation_level = Column(Integer, default=0)
    # Which copy variant produced this nudge. Without it the template A/B is
    # run and the results discarded.
    template_id = Column(String(60), default="")
    # Per-channel delivery state. `channel` stays for backwards compatibility;
    # these record what actually went out.
    email_status = Column(String(15), default="none")     # none|queued|sent|failed|skipped
    email_sent_at = Column(DateTime, nullable=True)
    whatsapp_status = Column(String(15), default="none")
    whatsapp_sent_at = Column(DateTime, nullable=True)
    whatsapp_template = Column(String(60), default="")
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
    # NEW: idempotency key. If a webhook retries with the same lecture_id,
    # process_attendance() returns early and does NOT increment counts.
    last_lecture_id = Column(String(100), default="")
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
    # Real mentor for this batch. Empty means "unknown" — alerts are skipped
    # rather than sent to a synthesised recipient nobody reads.
    mentor_id = Column(String(100), default="")
    uploaded_at = Column(DateTime, nullable=False)
    expected_by = Column(DateTime, nullable=True)
    watch_percent = Column(Integer, default=0)
    completed = Column(Boolean, default=False)
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
    scores_json = Column(JSON, default=lambda: [])
    latest_score = Column(Float, nullable=True)
    attempt_count = Column(Integer, default=0)
    batch_average = Column(Float, nullable=True)
    improvement_trend = Column(String(10), default="flat")
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    __table_args__ = (Index("idx_tp_uct", "user_id", "course_id", "topic_name", unique=True),)


# ========== DROPOUT FEATURES (for ML training) ==========
class StudentFeatures(Base):
    """Aggregated features for ML dropout prediction. Updated daily by cron
    (engine.aggregate_features_daily). All fields are populated — none of
    them stay at 0 forever."""
    __tablename__ = "nudge_student_features"
    user_id = Column(String(100), primary_key=True)
    course_id = Column(String(100), primary_key=True)
    login_frequency = Column(Float, default=0)
    avg_session_minutes = Column(Float, default=0)
    score_trend = Column(Float, default=0)
    consecutive_misses = Column(Integer, default=0)
    assignment_completion_rate = Column(Float, default=0)
    recording_completion_rate = Column(Float, default=0)
    days_since_last_login = Column(Integer, default=0)
    last_login_at = Column(DateTime, nullable=True)
    total_nudges_received = Column(Integer, default=0)
    nudge_response_rate = Column(Float, default=0)
    dropped_out = Column(Boolean, nullable=True)
    predicted_dropout_prob = Column(Float, nullable=True)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

# ========== CONTACTS & CONSENT ==========
class Contact(Base):
    """Where a person can be reached, and whether they agreed to it.

    Consent is stored per channel and defaults to False for anything outside
    the app. In-app needs no consent; email and WhatsApp do.
    """
    __tablename__ = "nudge_contacts"
    user_id = Column(String(100), primary_key=True)
    full_name = Column(String(200), default="")
    email = Column(String(200), default="")
    phone_e164 = Column(String(20), default="")
    email_opt_in = Column(Boolean, default=False)
    whatsapp_opt_in = Column(Boolean, default=False)
    quiet_hours_start = Column(Integer, nullable=True)
    quiet_hours_end = Column(Integer, nullable=True)
    unsubscribed_all = Column(Boolean, default=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


# ========== SCHEDULED CLASSES (for 60/30/15-minute reminders) ==========
class ScheduledClass(Base):
    """A class that is going to happen, so it can be reminded about.

    The agent previously only learned a lecture existed once attendance was
    marked — i.e. after it had started. This is the timetable.
    """
    __tablename__ = "nudge_scheduled_classes"
    id = Column(String(36), primary_key=True, default=uid)
    class_id = Column(String(100), nullable=False)
    course_id = Column(String(100), nullable=False)
    batch_id = Column(String(100), default="")
    title = Column(String(300), default="")
    starts_at = Column(DateTime, nullable=False)
    duration_minutes = Column(Integer, default=60)
    join_url = Column(String(500), default="")
    mentor_id = Column(String(100), default="")
    student_ids = Column(JSON, default=lambda: [])
    # Highest reminder tier already sent: 0 none, 60, 30, 15.
    reminded_at_tier = Column(Integer, default=0)
    cancelled = Column(Boolean, default=False)
    created_at = Column(DateTime, default=datetime.utcnow)
    __table_args__ = (
        Index("idx_sched_unique", "class_id", unique=True),
        Index("idx_sched_upcoming", "starts_at", "cancelled"),
    )


# ========== ABANDONED ACTIVITY ATTEMPTS ==========
class ActivityAttempt(Base):
    """Anything a student started and may not have finished.

    Covers tests, assessments, psychometrics, mock interviews, pulse quizzes,
    capstone drafts, moonshot days and profile completion — one table, keyed by
    activity_type, rather than one tracker per feature.
    """
    __tablename__ = "nudge_activity_attempts"
    id = Column(String(36), primary_key=True, default=uid)
    user_id = Column(String(100), nullable=False)
    course_id = Column(String(100), default="")
    activity_type = Column(String(40), nullable=False)
    activity_id = Column(String(100), nullable=False)
    activity_name = Column(String(300), default="")
    steps_done = Column(Integer, default=0)
    steps_total = Column(Integer, default=0)
    progress_percent = Column(Integer, default=0)
    resume_url = Column(String(500), default="")
    started_at = Column(DateTime, default=datetime.utcnow)
    last_seen_at = Column(DateTime, default=datetime.utcnow)
    expires_at = Column(DateTime, nullable=True)
    completed = Column(Boolean, default=False)
    completed_at = Column(DateTime, nullable=True)
    # Highest abandonment stage already nudged: 0 none, 1, 2, 3, 4.
    reminded_stage = Column(Integer, default=0)
    last_reminded_at = Column(DateTime, nullable=True)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    __table_args__ = (
        Index("idx_attempt_unique", "user_id", "activity_type", "activity_id", unique=True),
        Index("idx_attempt_open", "completed", "last_seen_at"),
    )


# ========== STREAKS ==========
class Streak(Base):
    """Consecutive-attendance streaks, per student per course."""
    __tablename__ = "nudge_streaks"
    user_id = Column(String(100), primary_key=True)
    course_id = Column(String(100), primary_key=True)
    current_classes = Column(Integer, default=0)
    longest_classes = Column(Integer, default=0)
    streak_started_at = Column(DateTime, nullable=True)
    last_attended_at = Column(DateTime, nullable=True)
    # Highest milestone already celebrated, so a 30-day streak is not
    # congratulated every single class.
    last_milestone = Column(Integer, default=0)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
