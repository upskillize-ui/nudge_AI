"""Shared route dependencies: authentication and error shaping."""
import hmac
import logging
from typing import Any, Dict

from fastapi import Header, HTTPException

from app.config import get_settings

log = logging.getLogger("routes")
settings = get_settings()


def verify_webhook(x_webhook_secret: str = Header(..., alias="X-Webhook-Secret")) -> None:
    """Reject requests without the shared LMS webhook secret."""
    if not hmac.compare_digest(x_webhook_secret, settings.lms_webhook_secret):
        raise HTTPException(401, "Invalid webhook secret")


def verify_api(x_api_key: str = Header(..., alias="X-API-Key")) -> None:
    """Reject requests without the agent API key."""
    if not hmac.compare_digest(x_api_key, settings.api_secret_key):
        raise HTTPException(401, "Invalid API key")


def error_response(code: str, detail: str) -> Dict[str, Any]:
    """Structured error body (Standards §5).

    Internal exception text never reaches the client — `detail` must be a
    message written for the caller, not a stack trace.
    """
    return {"error": code, "detail": detail}
