-- Nudge AI v2.3 migration. Run once on an existing Aiven MySQL database.
-- A fresh database gets all of this automatically via init_db().

-- Delivery state + copy-variant attribution on every nudge.
ALTER TABLE nudge_nudges
  ADD COLUMN template_id       VARCHAR(60) DEFAULT '',
  ADD COLUMN email_status      VARCHAR(15) DEFAULT 'none',
  ADD COLUMN email_sent_at     DATETIME NULL,
  ADD COLUMN whatsapp_status   VARCHAR(15) DEFAULT 'none',
  ADD COLUMN whatsapp_sent_at  DATETIME NULL,
  ADD COLUMN whatsapp_template VARCHAR(60) DEFAULT '';

-- Where a person can be reached, and what they consented to.
CREATE TABLE IF NOT EXISTS nudge_contacts (
  user_id           VARCHAR(100) PRIMARY KEY,
  full_name         VARCHAR(200) DEFAULT '',
  email             VARCHAR(200) DEFAULT '',
  phone_e164        VARCHAR(20)  DEFAULT '',
  email_opt_in      BOOLEAN      DEFAULT FALSE,
  whatsapp_opt_in   BOOLEAN      DEFAULT FALSE,
  quiet_hours_start INT NULL,
  quiet_hours_end   INT NULL,
  unsubscribed_all  BOOLEAN      DEFAULT FALSE,
  updated_at        DATETIME     DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
);

-- The timetable, so 60/30/15-minute reminders are possible at all.
CREATE TABLE IF NOT EXISTS nudge_scheduled_classes (
  id               VARCHAR(36) PRIMARY KEY,
  class_id         VARCHAR(100) NOT NULL,
  course_id        VARCHAR(100) NOT NULL,
  batch_id         VARCHAR(100) DEFAULT '',
  title            VARCHAR(300) DEFAULT '',
  starts_at        DATETIME NOT NULL,
  duration_minutes INT DEFAULT 60,
  join_url         VARCHAR(500) DEFAULT '',
  mentor_id        VARCHAR(100) DEFAULT '',
  student_ids      JSON,
  reminded_at_tier INT DEFAULT 0,
  cancelled        BOOLEAN DEFAULT FALSE,
  created_at       DATETIME DEFAULT CURRENT_TIMESTAMP,
  UNIQUE KEY idx_sched_unique (class_id),
  KEY idx_sched_upcoming (starts_at, cancelled)
);

-- Anything a student started and may not have finished. One table for tests,
-- assessments, psychometrics, mock interviews, pulse quizzes, capstone drafts,
-- moonshot days and profile completion.
CREATE TABLE IF NOT EXISTS nudge_activity_attempts (
  id               VARCHAR(36) PRIMARY KEY,
  user_id          VARCHAR(100) NOT NULL,
  course_id        VARCHAR(100) DEFAULT '',
  activity_type    VARCHAR(40)  NOT NULL,
  activity_id      VARCHAR(100) NOT NULL,
  activity_name    VARCHAR(300) DEFAULT '',
  steps_done       INT DEFAULT 0,
  steps_total      INT DEFAULT 0,
  progress_percent INT DEFAULT 0,
  resume_url       VARCHAR(500) DEFAULT '',
  started_at       DATETIME DEFAULT CURRENT_TIMESTAMP,
  last_seen_at     DATETIME DEFAULT CURRENT_TIMESTAMP,
  expires_at       DATETIME NULL,
  completed        BOOLEAN DEFAULT FALSE,
  completed_at     DATETIME NULL,
  reminded_stage   INT DEFAULT 0,
  last_reminded_at DATETIME NULL,
  updated_at       DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  UNIQUE KEY idx_attempt_unique (user_id, activity_type, activity_id),
  KEY idx_attempt_open (completed, last_seen_at)
);

-- Consecutive-attendance streaks.
CREATE TABLE IF NOT EXISTS nudge_streaks (
  user_id           VARCHAR(100) NOT NULL,
  course_id         VARCHAR(100) NOT NULL,
  current_classes   INT DEFAULT 0,
  longest_classes   INT DEFAULT 0,
  streak_started_at DATETIME NULL,
  last_attended_at  DATETIME NULL,
  last_milestone    INT DEFAULT 0,
  updated_at        DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (user_id, course_id)
);
