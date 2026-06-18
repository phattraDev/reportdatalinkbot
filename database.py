from sqlalchemy import create_engine, Column, Integer, String, DateTime, Text, Boolean, text
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker
from datetime import datetime
import os

# Use DATABASE_URL env var (Supabase/PostgreSQL) or fallback to SQLite for local dev
raw_url = os.getenv("DATABASE_URL", "sqlite:///./reports.db")
# Aggressively clean whitespace, newlines, quotes
DATABASE_URL = raw_url.strip().strip('"').strip("'").replace('\n', '').replace('\r', '')

# Rewrite URL for correct driver
if DATABASE_URL.startswith("postgresql://") or DATABASE_URL.startswith("postgresql+"):
    # Use pg8000 pure-python driver (works on Python 3.14)
    db_url = DATABASE_URL.replace("postgresql+psycopg://", "postgresql://", 1)
    db_url = db_url.replace("postgresql://", "postgresql+pg8000://", 1)
    engine = create_engine(db_url)
else:
    db_url = DATABASE_URL
    engine = create_engine(db_url, connect_args={"check_same_thread": False})
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


class Report(Base):
    __tablename__ = "reports"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(String, index=True)
    username = Column(String)
    target_name = Column(String, index=True)
    link = Column(Text)
    action = Column(String)
    action_detail = Column(String)
    raw_message = Column(Text)
    created_at = Column(DateTime, default=datetime.utcnow)


class KnownName(Base):
    __tablename__ = "known_names"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, unique=True, index=True)
    usage_count = Column(Integer, default=1)
    last_used = Column(DateTime, default=datetime.utcnow)


class BotUser(Base):
    __tablename__ = "bot_users"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(String, unique=True, index=True)
    username = Column(String)
    photo_url = Column(String, nullable=True)
    photo_file_id = Column(String, nullable=True)
    allowed = Column(Boolean, default=True)
    first_seen = Column(DateTime, default=datetime.utcnow)
    last_seen = Column(DateTime, default=datetime.utcnow)


class SummaryOverride(Base):
    __tablename__ = "summary_overrides"

    id = Column(Integer, primary_key=True, index=True)
    date_key = Column(String, unique=True, index=True)  # YYYY-MM-DD
    summary = Column(Text)
    updated_at = Column(DateTime, default=datetime.utcnow)


def init_db():
    Base.metadata.create_all(bind=engine)
    # Lightweight migrations for existing DBs
    with engine.connect() as conn:
        try:
            conn.execute(text("ALTER TABLE bot_users ADD COLUMN photo_url VARCHAR"))
            conn.commit()
        except Exception:
            pass  # column already exists
        try:
            conn.execute(text("ALTER TABLE bot_users ADD COLUMN photo_file_id VARCHAR"))
            conn.commit()
        except Exception:
            pass  # column already exists


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
