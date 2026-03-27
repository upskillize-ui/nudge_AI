import time, logging
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from app.config import get_settings
from app.database import init_db
from app.api.routes import wh, nf, ma
from app.core.scheduler import start_scheduler, stop_scheduler

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(name)-18s | %(levelname)-7s | %(message)s")
log = logging.getLogger("agent")
settings = get_settings()
templates = Jinja2Templates(directory="templates")
START = time.time()

@asynccontextmanager
async def lifespan(app: FastAPI):
    log.info(f"=== {settings.agent_name} starting ===")
    try: init_db(); log.info("DB tables ready")
    except Exception as e: log.error(f"DB init failed: {e}")
    start_scheduler()
    yield
    stop_scheduler()

app = FastAPI(title="Upskillize Nudge AI Agent", version="2.0.0", lifespan=lifespan,
              description="Hybrid Rules+AI notification engine for Upskillize LMS")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=True,
                   allow_methods=["*"], allow_headers=["*"])
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
    db_ok = False; pending = 0; today_n = 0
    try:
        db = SessionLocal(); db.execute(text("SELECT 1")); db_ok = True
        today = datetime.utcnow().replace(hour=0,minute=0,second=0)
        pending = db.query(func.count(Nudge.id)).filter(Nudge.status=="pending").scalar()
        today_n = db.query(func.count(Nudge.id)).filter(Nudge.created_at>=today).scalar()
        db.close()
    except: pass
    return {"status": "healthy" if db_ok else "degraded", "agent": settings.agent_name,
            "version": "2.0.0", "db": db_ok, "uptime": round(time.time()-START),
            "pending": pending, "today": today_n, "ai_enabled": settings.enable_dropout_prediction}
