"""
PATCH NOTES (v2.1):
- CORS: removed allow_origins=["*"] + allow_credentials=True combo (browser
  silently rejects this). Origins now driven by settings.allowed_origins.
- Lifespan: stop_scheduler is called even if init_db fails on startup.
- Health: bare `except: pass` replaced with logged exception.
- Daily aggregator scheduled here (uses `agg_features` job id).
"""
import time
import logging
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from app.config import get_settings
from app.database import init_db
from app.api.routes import wh, nf, ma
from app.core.scheduler import start_scheduler, stop_scheduler

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(name)-18s | %(levelname)-7s | %(message)s",
)
log = logging.getLogger("agent")
settings = get_settings()
templates = Jinja2Templates(directory="templates")
START = time.time()


@asynccontextmanager
async def lifespan(app: FastAPI):
    log.info(f"=== {settings.agent_name} starting ===")
    scheduler_started = False
    try:
        try:
            init_db()
            log.info("DB tables ready")
        except Exception as e:
            log.error(f"DB init failed: {e}")
        try:
            start_scheduler()
            scheduler_started = True
        except Exception as e:
            log.error(f"Scheduler start failed: {e}")
        yield
    finally:
        if scheduler_started:
            try:
                stop_scheduler()
            except Exception as e:
                log.error(f"Scheduler stop failed: {e}")


app = FastAPI(
    title="Upskillize Nudge AI Agent",
    version="2.1.0",
    lifespan=lifespan,
    description="Hybrid Rules+AI notification engine for Upskillize LMS",
)

# CORS — locked. Add your real LMS origins here. Wildcard + credentials is invalid per spec.
_default_origins = [
    "https://upskillize.com",
    "https://www.upskillize.com",
    "https://lms.upskillize.com",
    "https://upskillize-lms-backend.onrender.com",
    "http://localhost:5173",  # Vite dev
    "http://localhost:3000",
]
allowed_origins = getattr(settings, "allowed_origins", None) or _default_origins
app.add_middleware(
    CORSMiddleware,
    allow_origins=allowed_origins,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PATCH", "OPTIONS"],
    allow_headers=["Content-Type", "X-API-Key", "X-Webhook-Secret"],
)

app.include_router(wh, prefix="/api/v1")
app.include_router(nf, prefix="/api/v1")
app.include_router(ma, prefix="/api/v1")


@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    return templates.TemplateResponse("dashboard.html", {"request": request})


@app.get("/health")
async def health():
    from app.database import SessionLocal
    from app.models import Nudge
    from sqlalchemy import func, text
    from datetime import datetime
    db_ok = False
    pending = 0
    today_n = 0
    try:
        db = SessionLocal()
        try:
            db.execute(text("SELECT 1"))
            db_ok = True
            today = datetime.utcnow().replace(hour=0, minute=0, second=0)
            pending = db.query(func.count(Nudge.id)).filter(Nudge.status == "pending").scalar()
            today_n = db.query(func.count(Nudge.id)).filter(Nudge.created_at >= today).scalar()
        finally:
            db.close()
    except Exception as e:
        log.error(f"Health check DB error: {e}")
    return {
        "status": "healthy" if db_ok else "degraded",
        "agent": settings.agent_name,
        "version": "2.1.0",
        "db": db_ok,
        "uptime": round(time.time() - START),
        "pending": pending,
        "today": today_n,
        "ai_enabled": settings.enable_dropout_prediction,
    }