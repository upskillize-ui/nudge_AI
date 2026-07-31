"""Admission control — a full house answers honestly instead of hanging.

When every slot in this worker is busy, a new request gets an immediate,
student-readable 503 ("all sessions are occupied right now — please try
again in a few minutes") instead of joining an invisible queue that ends
in a timeout. A visible "try again shortly" keeps students calm; a frozen
spinner makes them refresh, which doubles the load at the worst moment.

Per-process counter by design: the cap multiplies by the number of uvicorn
workers, so the Space-wide ceiling is max_concurrent_requests x workers.
No locks across workers, no shared state, nothing to break.
"""
import json
from typing import Iterable

#: Paths that must never be refused — health checks and the keep-warm ping.
EXEMPT_PATHS = ("/health", "/")


class AdmissionControl:
    """Pure ASGI middleware: cap concurrent in-flight HTTP requests."""

    def __init__(self, app, max_concurrent: int, exempt: Iterable[str] = EXEMPT_PATHS):
        self.app = app
        self.max_concurrent = int(max_concurrent)
        self.exempt = tuple(exempt)
        self._inflight = 0

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope.get("path", "") in self.exempt:
            await self.app(scope, receive, send)
            return

        if self._inflight >= self.max_concurrent:
            body = json.dumps({
                "error": "all_sessions_occupied",
                "message": (
                    "All sessions are occupied just for a moment - a spot "
                    "opens up shortly. Please try again in a few minutes."
                ),
                "retry_after_seconds": 60,
            }).encode()
            await send({
                "type": "http.response.start",
                "status": 503,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"retry-after", b"60"),
                    (b"content-length", str(len(body)).encode()),
                ],
            })
            await send({"type": "http.response.body", "body": body})
            return

        self._inflight += 1
        try:
            await self.app(scope, receive, send)
        finally:
            self._inflight -= 1
