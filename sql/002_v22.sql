-- Nudge AI v2.2 migration. Run once on an existing Aiven MySQL database.
-- A fresh database gets these automatically via init_db().

-- v2.1: idempotency key for attendance webhook retries.
ALTER TABLE nudge_attendance
  ADD COLUMN last_lecture_id VARCHAR(100) DEFAULT '';

-- v2.2: real mentor for the batch, so overdue-recording and dropout alerts
-- reach a person instead of a synthesised "mentor_{batch}" recipient.
ALTER TABLE nudge_recordings
  ADD COLUMN mentor_id VARCHAR(100) DEFAULT '';

-- v2.2: real last-login timestamp. days_since_last_login is now derived from
-- this instead of being overwritten nightly from attendance activity.
ALTER TABLE nudge_student_features
  ADD COLUMN last_login_at DATETIME NULL;
