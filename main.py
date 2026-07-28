"""Application factory and startup wiring.

Everything else lives in app/. This file only builds the app, mounts routers,
and manages the scheduler lifecycle.
"""
import logging
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from app.config import get_settings
from app.database import init_db
from app.routes import admin, feed, webhooks
from app.services.scheduler import start_scheduler, stop_scheduler

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(name)-22s | %(levelname)-7s | %(message)s",
)
log = logging.getLogger("agent")

settings = get_settings()
templates = Jinja2Templates(directory="templates")
STARTED_AT = time.time()

VERSION = "2.2.0"
API_PREFIX = "/api/v1"

#: Fallback CORS origins when ALLOWED_ORIGINS_RAW is unset. A wildcard is
#: invalid alongside allow_credentials, so the list is always explicit.
DEFAULT_ORIGINS = [
    "https://upskillize.com",
    "https://www.upskillize.com",
    "https://lms.upskillize.com",
    "https://upskillize-lms-backend.onrender.com",
    "http://localhost:5173",
    "http://localhost:3000",
]


@asynccontextmanager
async def lifespan(_app: FastAPI):
    """Initialise the database and scheduler, and shut the scheduler down."""
    log.info("=== %s %s starting ===", settings.agent_name, VERSION)
    scheduler_started = False
    try:
        try:
            init_db()
            log.info("DB tables ready")
        except Exception as exc:  # noqa: BLE001 — startup must survive DB outage
            log.error("DB init failed: %s", exc)
        try:
            start_scheduler()
            scheduler_started = True
        except Exception as exc:  # noqa: BLE001
            log.error("Scheduler start failed: %s", exc)
        yield
    finally:
        if scheduler_started:
            try:
                stop_scheduler()
            except Exception as exc:  # noqa: BLE001
                log.error("Scheduler stop failed: %s", exc)


def create_app() -> FastAPI:
    """Build the FastAPI application."""
    app = FastAPI(
        title="Upskillize Nudge AI Agent",
        version=VERSION,
        lifespan=lifespan,
        description="Hybrid Rules+AI engagement engine for Upskillize EcoPro LMS",
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.allowed_origins or DEFAULT_ORIGINS,
        allow_credentials=True,
        allow_methods=["GET", "POST", "PATCH", "OPTIONS"],
        allow_headers=["Content-Type", "X-API-Key", "X-Webhook-Secret"],
    )

    for router in (webhooks.router, feed.router, admin.router):
        app.include_router(router, prefix=API_PREFIX)

    return app


app = create_app()


@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    """Serve the operator dashboard."""
    return templates.TemplateResponse("dashboard.html", {"request": request})


@app.get("/health")
async def health():
    """Liveness and readiness, including a real database probe."""
    from sqlalchemy import func, text

    from app.database import SessionLocal
    from app.models import Nudge
    from app.utils.timezone import ist_today_start_utc

    db_ok, pending, today = False, 0, 0
    try:
        db = SessionLocal()
        try:
            db.execute(text("SELECT 1"))
            db_ok = True
            pending = db.query(func.count(Nudge.id)).filter(
                Nudge.status == "pending"
            ).scalar() or 0
            today = db.query(func.count(Nudge.id)).filter(
                Nudge.created_at >= ist_today_start_utc()
            ).scalar() or 0
        finally:
            db.close()
    except Exception as exc:  # noqa: BLE001 — health must never raise
        log.error("Health check DB error: %s", exc)

    return {
        "status": "healthy" if db_ok else "degraded",
        "agent": settings.agent_name,
        "version": VERSION,
        "db": db_ok,
        "uptime": round(time.time() - STARTED_AT),
        "pending": pending,
        "today": today,
        "ai_enabled": settings.enable_dropout_prediction,
    }
