# Upskillize Nudge AI Agent — Step-by-Step Setup Guide

## Architecture
```
NETLIFY (frontend)          RENDER (backend)           HUGGINGFACE (agent)
- LMS UI                    - LMS API                  - Nudge AI Engine
- Notification bell   <-->  - Proxy routes        <--> - Rules (80%)
- Student dashboard         - Webhook senders          - AI/ML (20%)
                                                       - Dashboard UI
                                                       - Background scheduler
                                                            |
                                                       AIVEN (MySQL)
                                                       - 8 auto-created tables
```

## STEP 1: Create MySQL Database (Aiven Cloud)

1. Go to https://aiven.io → Sign up (free, no credit card)
2. Create Service → MySQL → Free plan
3. Note your connection details:
   - Host: `mysql-xxxx.aiven.io`
   - Port: `3306`
   - User: `avnadmin`
   - Password: (shown once)
   - Database: `defaultdb`
4. Your DATABASE_URL will be:
   `mysql+pymysql://avnadmin:YOUR_PASSWORD@mysql-xxxx.aiven.io:3306/defaultdb`

**Tables are auto-created on first agent startup. No manual SQL needed.**

8 tables created automatically:
- `nudge_nudges` — all generated nudges
- `nudge_events` — analytics events
- `nudge_attendance` — live lecture attendance tracking
- `nudge_recordings` — recorded lecture watch tracking (NEW)
- `nudge_assignments` — assignment view/submit tracking
- `nudge_topics` — topic-wise performance scores
- `nudge_student_features` — ML features for dropout prediction
- `nudge_preferences` — (future) per-user notification settings


## STEP 2: Deploy Agent to HuggingFace Spaces

1. Go to https://huggingface.co/spaces → Create new Space
2. Name: `upskillize-nudge-ai`, SDK: `Docker`, Visibility: `Private`
3. Clone the space repo locally:
   ```bash
   git clone https://huggingface.co/spaces/YOUR_USERNAME/upskillize-nudge-ai
   ```
4. Copy ALL files from the `nudge-agent/` folder (except `lms_integration/`) into it
5. Push:
   ```bash
   git add . && git commit -m "Deploy Nudge AI" && git push
   ```
6. Go to Space Settings → Repository Secrets → Add:
   | Secret | Value |
   |--------|-------|
   | DATABASE_URL | mysql+pymysql://avnadmin:pass@mysql-xxx.aiven.io:3306/defaultdb |
   | API_SECRET_KEY | (run: `openssl rand -hex 32`) |
   | LMS_WEBHOOK_SECRET | (run: `openssl rand -hex 16`) |
   | ENVIRONMENT | production |

7. Wait for build (~3-5 min). Then visit:
   - `https://YOUR-SPACE.hf.space/` → Dashboard UI
   - `https://YOUR-SPACE.hf.space/health` → Health check JSON
   - `https://YOUR-SPACE.hf.space/docs` → Swagger API docs


## STEP 3: Add Integration to Render Backend

1. Copy `lms_integration/nudge_integration.js` to your project

2. Add env vars to Render:
   | Variable | Value |
   |----------|-------|
   | NUDGE_AGENT_URL | https://YOUR-SPACE.hf.space |
   | NUDGE_API_KEY | (same as API_SECRET_KEY from Step 2) |
   | NUDGE_WEBHOOK_SECRET | (same as LMS_WEBHOOK_SECRET from Step 2) |

3. In your `app.js` or `server.js`:
   ```javascript
   const { nudgeRouter, nudge } = require('./nudge_integration');
   app.use(nudgeRouter);
   ```

4. Add webhook calls to your existing functions. Here's EXACTLY where:

### 4a. Attendance Recording
```javascript
// In your existing function that marks attendance:
async function recordAttendance(studentId, lectureId, present) {
    // ... your existing code ...
    await saveToDatabase(studentId, lectureId, present);

    // ADD THIS LINE:
    await nudge.attendance(studentId, courseId, batchId, present, 'React Hooks', mentorId, 'Priya');
}
```

### 4b. Recording Upload
```javascript
// When mentor uploads a lecture recording:
async function uploadRecording(mentorId, courseId, lectureId, videoUrl) {
    // ... your existing upload code ...

    // ADD THIS:
    const studentIds = await getStudentIdsForBatch(batchId);
    const oneWeekLater = new Date(Date.now() + 7*24*60*60*1000).toISOString();
    await nudge.recordingUploaded(lectureId, courseId, batchId, 'React Hooks',
        videoUrl, new Date().toISOString(), oneWeekLater, studentIds);
}
```

### 4c. Recording Watch Progress
```javascript
// In your video player's progress event handler:
// Fire at 25%, 50%, 80%, 100% thresholds
videoPlayer.on('progress', (percent) => {
    if ([25, 50, 80, 100].includes(Math.floor(percent))) {
        nudge.recordingWatched(currentUserId, lectureId, Math.floor(percent));
    }
});
```

### 4d. Assignment Upload, View, Submit
```javascript
// When mentor creates assignment:
await nudge.assignmentUploaded(assignmentId, courseId, title, deadline.toISOString(), studentIds);

// When student opens assignment page:
await nudge.assignmentViewed(assignmentId, currentUserId);

// When student submits:
await nudge.assignmentSubmitted(assignmentId, currentUserId);
```

### 4e. Quiz/Assessment Scored
```javascript
// After auto-grading or manual scoring:
await nudge.quizScored(studentId, courseId, 'SQL Joins', 35, batchAverage, 'Priya', mentorId);
```

### 4f. Student Login (for ML data collection)
```javascript
// On successful login:
await nudge.login(userId, courseId, estimatedSessionMinutes);
```


## STEP 4: Add Notification Bell to Netlify Frontend

1. Copy content from `lms_integration/notification_bell.html` into your project

2. In your navbar/header component, add:
   ```html
   <div id="nudge-bell" style="position: relative;"></div>
   ```

3. At the bottom of your page (after user is logged in):
   ```html
   <script>
   window._nb = new NudgeBell({
       container: '#nudge-bell',
       apiBase: 'https://your-backend.onrender.com/api/nudges',
       userId: currentUser.id,
       userRole: currentUser.role    // 'student' or 'mentor' or 'admin'
   });
   window._nb.init();
   </script>
   ```


## STEP 5: Test Everything

### Test 1: Health Check
```bash
curl https://YOUR-SPACE.hf.space/health
```

### Test 2: Simulate a Student Missing Class
```bash
curl -X POST https://YOUR-SPACE.hf.space/api/v1/webhook/lecture-attendance \
  -H "Content-Type: application/json" \
  -H "X-Webhook-Secret: YOUR_SECRET" \
  -d '{"user_id":"student_001","course_id":"react_2026","batch_id":"batch_q1","attended":false,"lecture_title":"React Hooks"}'
```
Run this 3 times to trigger the 3-miss escalation.

### Test 3: Check the Nudge Was Created
```bash
curl "https://YOUR-SPACE.hf.space/api/v1/nudges/feed?user_id=student_001&role=student" \
  -H "X-API-Key: YOUR_API_KEY"
```

### Test 4: Upload a Recording and Check Tracking
```bash
curl -X POST https://YOUR-SPACE.hf.space/api/v1/webhook/recording-uploaded \
  -H "Content-Type: application/json" \
  -H "X-Webhook-Secret: YOUR_SECRET" \
  -d '{"lecture_id":"lec_001","course_id":"react_2026","batch_id":"batch_q1","lecture_title":"React Hooks","recording_url":"https://your-cdn.com/video.mp4","uploaded_at":"2026-03-25T10:00:00","expected_by":"2026-04-01T23:59:00","student_ids":["student_001","student_002"]}'
```

### Test 5: View the Dashboard
Open `https://YOUR-SPACE.hf.space/?key=YOUR_API_KEY` in your browser.


## HOW ATTENDANCE TRACKING WORKS

### Live Lectures:
1. Mentor marks attendance in LMS → LMS sends webhook → Agent updates `nudge_attendance` table
2. If attended: consecutive_misses resets to 0
3. If missed: consecutive_misses increments by 1
4. At 1 miss: info nudge to student
5. At 2 misses: high priority nudge to student
6. At 3+ misses: critical nudge to student + alert to mentor
7. At 5+ misses: alert to everyone including institute

### Recorded Lectures:
1. Recording uploaded → LMS sends webhook → Agent creates row per student in `nudge_recordings`
2. Student watches video → LMS sends watch progress → Agent updates watch_percent
3. When watch_percent >= 80% → marked as completed
4. Background scheduler runs every 30 minutes:
   - Finds recordings past expected_by date that aren't completed
   - Sends reminder nudges (max 3 per recording)
   - If student has 3+ unwatched recordings → alerts mentor
5. Dashboard shows: who watched, who didn't, how much they watched


## ENABLING AI (Month 7+)

After collecting 6 months of data:

1. Label your data: go through student records and mark who dropped out
   ```sql
   UPDATE nudge_student_features SET dropped_out = true WHERE user_id = 'student_who_left';
   UPDATE nudge_student_features SET dropped_out = false WHERE user_id = 'student_who_completed';
   ```

2. Train the model:
   ```bash
   curl -X POST https://YOUR-SPACE.hf.space/api/v1/admin/train-dropout \
     -H "X-API-Key: YOUR_KEY"
   ```

3. Enable predictions: Set `ENABLE_DROPOUT_PREDICTION=true` in HuggingFace secrets

4. The agent will now run dropout prediction weekly and alert mentors for high-risk students.

**Cost of AI features: $0 extra.** XGBoost runs on CPU, model is ~50KB.
