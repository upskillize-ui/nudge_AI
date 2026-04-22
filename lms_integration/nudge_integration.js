/**
 * ============================================================
 * ADD TO YOUR RENDER BACKEND (Express.js)
 * ============================================================
 *
 * 1. Copy this file to your project
 * 2. Add env vars: NUDGE_AGENT_URL, NUDGE_API_KEY, NUDGE_WEBHOOK_SECRET
 * 3. In your app.js: const {nudgeRouter, nudge} = require('./nudge_integration');
 *                    app.use(nudgeRouter);
 * 4. Then call nudge.xxx() from your existing functions (see WHERE TO CALL comments)
 */

const express = require('express');
const nudgeRouter = express.Router();

const AGENT = process.env.NUDGE_AGENT_URL || 'https://your-space.hf.space';
const KEY = process.env.NUDGE_API_KEY || 'your-key';
const SECRET = process.env.NUDGE_WEBHOOK_SECRET || 'your-secret';

async function post(endpoint, body, isWebhook = false) {
  const h = { 'Content-Type': 'application/json',
    ...(isWebhook ? { 'X-Webhook-Secret': SECRET } : { 'X-API-Key': KEY }) };
  try {
    const r = await fetch(`${AGENT}/api/v1/${endpoint}`, { method: 'POST', headers: h, body: JSON.stringify(body) });
    return r.json();
  } catch (e) { console.error(`Nudge ${endpoint}:`, e.message); return { error: e.message }; }
}
async function get(endpoint, params = {}) {
  const qs = new URLSearchParams(params).toString();
  try {
    const r = await fetch(`${AGENT}/api/v1/${endpoint}?${qs}`, { headers: { 'X-API-Key': KEY } });
    return r.json();
  } catch (e) { return { error: e.message }; }
}

// ========== WEBHOOK SENDERS (call from your existing LMS functions) ==========

const nudge = {
  // WHERE TO CALL: In your attendance recording function
  // After: markAttendance(studentId, lectureId, present)
  // Add:   nudge.attendance(studentId, courseId, batchId, present, 'React Hooks', mentorId, 'Priya')
  attendance: (userId, courseId, batchId, attended, title='', mentorId='', name='') =>
    post('webhook/lecture-attendance', { user_id: userId, course_id: courseId, batch_id: batchId,
      attended, lecture_title: title, mentor_id: mentorId, student_name: name }, true),

  // WHERE TO CALL: When mentor uploads a recording
  // After: uploadRecording(lectureId, videoUrl)
  // Add:   nudge.recordingUploaded(lectureId, courseId, batchId, title, url, now, weekFromNow, studentIds)
  recordingUploaded: (lectureId, courseId, batchId, title, url, uploadedAt, expectedBy, studentIds) =>
    post('webhook/recording-uploaded', { lecture_id: lectureId, course_id: courseId, batch_id: batchId,
      lecture_title: title, recording_url: url, uploaded_at: uploadedAt, expected_by: expectedBy,
      student_ids: studentIds }, true),

  // WHERE TO CALL: When student watches a recording (fire at 25%, 50%, 80%, 100%)
  // In your video player's progress event:
  // Add:   nudge.recordingWatched(studentId, lectureId, percentWatched)
  recordingWatched: (userId, lectureId, watchPercent) =>
    post('webhook/recording-watched', { user_id: userId, lecture_id: lectureId, watch_percent: watchPercent }, true),

  // WHERE TO CALL: When mentor creates an assignment
  assignmentUploaded: (assignmentId, courseId, title, deadline, studentIds, type='assignment') =>
    post('webhook/assignment-uploaded', { assignment_id: assignmentId, course_id: courseId,
      title, deadline, student_ids: studentIds, assignment_type: type, closes_after_deadline: true }, true),

  // WHERE TO CALL: When student opens assignment page
  assignmentViewed: (assignmentId, userId) =>
    post('webhook/assignment-viewed', { assignment_id: assignmentId, user_id: userId }, true),

  // WHERE TO CALL: When student submits
  assignmentSubmitted: (assignmentId, userId) =>
    post('webhook/assignment-submitted', { assignment_id: assignmentId, user_id: userId }, true),

  // WHERE TO CALL: After quiz auto-grading or manual scoring
  quizScored: (userId, courseId, topic, score, batchAvg=null, name='', mentorId='') =>
    post('webhook/quiz-scored', { user_id: userId, course_id: courseId, topic_name: topic,
      score, batch_average: batchAvg, student_name: name, mentor_id: mentorId }, true),

  // WHERE TO CALL: On student login (for ML data collection)
  login: (userId, courseId='', sessionMinutes=0) =>
    post('webhook/login', { user_id: userId, course_id: courseId, session_minutes: sessionMinutes }, true),
};

// ========== PROXY ROUTES (your Netlify frontend calls these) ==========

// Notification bell feed
nudgeRouter.get('/api/nudges/feed', async (req, res) => {
  const d = await get('nudges/feed', { user_id: req.query.user_id, role: req.query.role || 'student',
    status: req.query.status || 'unread', limit: req.query.limit || 20 });
  res.json(d);
});

// Badge count
nudgeRouter.get('/api/nudges/unread-count', async (req, res) => {
  res.json(await get('nudges/unread-count', { user_id: req.query.user_id, role: req.query.role || 'student' }));
});

// Mark status
nudgeRouter.patch('/api/nudges/:id/status', async (req, res) => {
  const r = await fetch(`${AGENT}/api/v1/nudges/${req.params.id}/status`,
    { method: 'PATCH', headers: { 'X-API-Key': KEY, 'Content-Type': 'application/json' }, body: JSON.stringify(req.body) });
  res.json(await r.json());
});

// Batch read
nudgeRouter.post('/api/nudges/batch-read', async (req, res) => {
  res.json(await post(`nudges/batch-read?user_id=${req.query.user_id}`, {}));
});

// Mentor dashboard
nudgeRouter.get('/api/nudges/mentor/critical', async (req, res) => {
  res.json(await get('mentor/critical-students', { batch_id: req.query.batch_id || '' }));
});

// Student improvements
nudgeRouter.get('/api/nudges/student/improvements', async (req, res) => {
  res.json(await get('student/improvements', { user_id: req.query.user_id }));
});

// Attendance report
nudgeRouter.get('/api/nudges/reports/attendance', async (req, res) => {
  res.json(await get('reports/attendance', { course_id: req.query.course_id||'', batch_id: req.query.batch_id||'' }));
});

// Recording report
nudgeRouter.get('/api/nudges/reports/recordings', async (req, res) => {
  res.json(await get('reports/recordings', { course_id: req.query.course_id||'', batch_id: req.query.batch_id||'' }));
});

module.exports = { nudgeRouter, nudge };