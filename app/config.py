from pydantic_settings import BaseSettings
from functools import lru_cache

class Settings(BaseSettings):
    database_url: str = "mysql+pymysql://root:pass@localhost:3306/upskillize_nudge"
    api_secret_key: str = "change-me-32-chars-minimum-secret"
    lms_webhook_secret: str = "shared-secret"
    lms_backend_url: str = "http://localhost:8000"
    agent_name: str = "Upskillize Nudge AI"
    environment: str = "development"
    max_nudges_per_day: int = 8
    nudge_check_interval_minutes: int = 15
    quiet_hours_start: int = 22
    quiet_hours_end: int = 7
    timezone: str = "Asia/Kolkata"
    # AI/ML settings
    enable_dropout_prediction: bool = False  # Enable after 6 months of data
    dropout_model_path: str = "models/dropout_model.json"
    dropout_threshold: float = 0.70
    min_training_records: int = 500

    class Config:
        env_file = ".env"

@lru_cache()
def get_settings() -> Settings:
    return Settings()
