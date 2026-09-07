"""
db.py
Database connection, session factory, and schema initialisation.
"""

import os
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, Session
from contextlib import contextmanager

from models import Base

DATABASE_URL = os.environ.get(
    "DATABASE_URL",
    "postgresql://portracker:portracker@localhost:5432/portracker"
)

# Railway injects postgres:// — SQLAlchemy needs postgresql://
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

engine = create_engine(DATABASE_URL, pool_pre_ping=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)


def init_db():
    """Create all tables if they don't exist."""
    Base.metadata.create_all(bind=engine)


@contextmanager
def get_db() -> Session:
    """Context manager for a database session."""
    db = SessionLocal()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def get_db_dep():
    """FastAPI dependency — yields a session and closes it after the request."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()     