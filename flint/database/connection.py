"""
Flint — Database Connection
SQLAlchemy session factory. Works with PostgreSQL (production) and SQLite (local testing).
"""

import os
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from .models import Base
# Import workspace models so SQLAlchemy registers them with Base
from ..workspace import models as _workspace_models  # noqa: F401
from ..referral import models as _referral_models    # noqa: F401
from ..livestream import models as _livestream_models  # noqa: F401
from ..payouts import models as _payouts_models        # noqa: F401
from ..affiliate import models as _affiliate_models      # noqa: F401

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./flintx_local.db")

# PostgreSQL from Supabase uses postgres:// — SQLAlchemy needs postgresql://
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

engine = create_engine(
    DATABASE_URL,
    pool_pre_ping   = True,
    pool_size       = 10 if "postgresql" in DATABASE_URL else 1,
    max_overflow    = 20 if "postgresql" in DATABASE_URL else 0,
    connect_args    = {"check_same_thread": False} if "sqlite" in DATABASE_URL else {},
)

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


def create_tables():
    Base.metadata.create_all(bind=engine)


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
