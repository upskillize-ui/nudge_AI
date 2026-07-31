"""
PATCH NOTES (v2.1):
- Production startup now ASSERTS real secrets (no more 'change-me' in prod).
- allowed_origins setting added (consumed by main.py CORS config).
- channel preferences exposed (in_app, email, push, sms) for future delivery work.
"""
from pydantic_settings import BaseSettings
from functools import lru_cache
from typing import List


class Settings(BaseSettings):
    database_url: str = "mysql+pymysql://root:pass@localhost:3306/upskillize_nudge"
    api_secret_key: str = "change-me-32-chars-minimum-secret"
    lms_webhook_secret: str = "shared-secret"
    lms_backend_url: str = "http://localhost:8000"
    agent_name: str = "Upskillize Nudge AI"
    environment: str = "development"
    max_nudges_per_day: int = 8
    nudge_check_interval_minutes: int = 15
    #: Localhost port used only as a cross-worker lock for scheduler election.
    scheduler_lock_port: int = 7899
    quiet_hours_start: int = 22
    quiet_hours_end: int = 7
    timezone: str = "Asia/Kolkata"

    # CORS allow-list (comma-separated string in env, parsed below)
    allowed_origins_raw: str = ""

    # Copy generation. OFF by default: templates handle ~95% of nudges at zero
    # cost, and AI is only worth spending on the handful of cases listed in
    # services/copy.AI_ELIGIBLE_TYPES.
    enable_ai_copy: bool = False
    ai_copy_model: str = "claude-haiku-4-5"

    # AI/ML settings
    enable_dropout_prediction: bool = False
    dropout_model_path: str = "models/dropout_model.json"
    dropout_threshold: float = 0.70
    min_training_records: int = 500

    # Channel toggles (for future multi-channel delivery)
    enable_email_channel: bool = False
    enable_push_channel: bool = False
    enable_sms_channel: bool = False

    class Config:
        env_file = ".env"

    @property
    def allowed_origins(self) -> List[str]:
        if not self.allowed_origins_raw:
            return []
        return [o.strip() for o in self.allowed_origins_raw.split(",") if o.strip()]


@lru_cache()
def get_settings() -> Settings:
    s = Settings()
    # Production sanity checks — fail fast, do not run with default secrets.
    if s.environment.lower() == "production":
        if s.api_secret_key.startswith("change-me"):
            raise RuntimeError(
                "API_SECRET_KEY is the default placeholder. "
                "Set a real secret in environment before starting in production."
            )
        if s.lms_webhook_secret == "shared-secret":
            raise RuntimeError(
                "LMS_WEBHOOK_SECRET is the default placeholder. "
                "Set a real secret in environment before starting in production."
            )
    return s