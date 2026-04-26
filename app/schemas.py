"""
PATCH NOTES (v2.1):
- LectureAttendanceEvent.lecture_id is the new idempotency key (was: empty string)
- ManualNudgeRequest.severity added (was: hardcoded to 'info' on backend)
"""
from pydantic import BaseModel
from typing import Optional, List


# ===== WEBHOOK EVENTS (LMS -> Agent) =====
class LectureAttendanceEvent(BaseModel):
    user_id: str
    course_id: str
    batch_id: str = ""
    lecture_id: str = ""  # used as idempotency key in process_attendance
    attended: bool
    lecture_title: str = ""
    mentor_id: str = ""
    student_name: str = ""


class RecordingUploadEvent(BaseModel):
    lecture_id: str
    course_id: str
    batch_id: str = ""
    lecture_title: str = ""
    recording_url: str = ""
    uploaded_at: str = ""
    expected_by: str = ""
    student_ids: List[str] = []


class RecordingWatchEvent(BaseModel):
    user_id: str
    lecture_id: str
    watch_percent: int = 0


class AssignmentUploadEvent(BaseModel):
    assignment_id: str
    course_id: str
    title: str
    deadline: str
    student_ids: List[str] = []
    assignment_type: str = "assignment"
    closes_after_deadline: bool = True


class AssignmentViewEvent(BaseModel):
    assignment_id: str
    user_id: str


class AssignmentSubmitEvent(BaseModel):
    assignment_id: str
    user_id: str


class QuizScoreEvent(BaseModel):
    user_id: str
    course_id: str
    topic_name: str
    score: float
    batch_average: Optional[float] = None
    student_name: str = ""
    mentor_id: str = ""


class AssignmentGradedEvent(BaseModel):
    assignment_id: str
    user_id: str
    score: float
    weak_areas: List[str] = []
    strong_areas: List[str] = []


class StatusUpdate(BaseModel):
    status: str


class ManualNudgeRequest(BaseModel):
    user_ids: List[str]
    title: str
    body: str
    priority: str = "medium"
    severity: str = "info"  # NEW: was hardcoded server-side
    nudge_type: str = "custom"
    cta_text: str = ""
    cta_url: str = ""


class LoginEvent(BaseModel):
    user_id: str
    course_id: str = ""
    session_minutes: float = 0