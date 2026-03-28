from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, declarative_base
from app.config import get_settings

settings = get_settings()

# Add charset=utf8mb4 to support emoji in message templates
db_url = settings.database_url
if "mysql" in db_url and "charset" not in db_url:
    separator = "&" if "?" in db_url else "?"
    db_url = db_url + separator + "charset=utf8mb4"

engine = create_engine(db_url, pool_size=10, max_overflow=20, pool_recycle=3600, pool_pre_ping=True, echo=False)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

def init_db():
    Base.metadata.create_all(bind=engine)