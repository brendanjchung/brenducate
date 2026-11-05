"""
AgentPulse Ops — Database Layer
SQLite via SQLAlchemy (zero config — just works out of the box for MVP).
Swap DATABASE_URL in .env to postgres:// to go to production.
"""

from sqlalchemy import (
    create_engine, Column, String, Float, Integer,
    Boolean, DateTime, Text
)
from sqlalchemy.orm import declarative_base, sessionmaker
from datetime import datetime, timezone
import os

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./agentpulse.db")

# SQLite needs check_same_thread=False for multi-threaded FastAPI
connect_args = {"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {}

engine = create_engine(DATABASE_URL, connect_args=connect_args)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


# ── ORM Models ──────────────────────────────────────────────────────────────

class ApiKey(Base):
    __tablename__ = "api_keys"

    key                    = Column(String, primary_key=True, index=True)
    team_name              = Column(String, nullable=False)
    email                  = Column(String, nullable=True)
    plan                   = Column(String, default="starter")   # starter | pro | scale
    stripe_customer_id     = Column(String, nullable=True)
    stripe_subscription_id = Column(String, nullable=True)
    created_at             = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    active                 = Column(Boolean, default=True)


class Trace(Base):
    __tablename__ = "traces"

    id                = Column(String, primary_key=True, index=True)
    api_key           = Column(String, index=True)
    agent_id          = Column(String, nullable=False)
    model             = Column(String, nullable=False)
    prompt            = Column(Text, nullable=False)
    status            = Column(String, default="COMPLETED")  # COMPLETED | CIRCUIT_KILLED | DRIFT_WARNING
    input_tokens      = Column(Integer, default=0)
    output_tokens     = Column(Integer, default=0)
    total_tokens      = Column(Integer, default=0)
    cost_usd          = Column(Float, default=0.0)
    latency_ms        = Column(Integer, default=0)
    hops              = Column(Integer, default=1)
    circuit_tripped   = Column(Boolean, default=False)
    guardrail_reason  = Column(String, nullable=True)
    steps_json        = Column(Text, nullable=True)   # JSON blob of step-by-step trace
    environment       = Column(String, default="production")
    created_at        = Column(DateTime, default=lambda: datetime.now(timezone.utc))


class Alert(Base):
    __tablename__ = "alerts"

    id           = Column(Integer, primary_key=True, autoincrement=True)
    api_key      = Column(String, index=True)
    alert_type   = Column(String)   # CIRCUIT_BREAKER | DRIFT | COST_THRESHOLD | PII
    agent_id     = Column(String)
    message      = Column(Text)
    severity     = Column(String, default="warning")   # info | warning | critical
    resolved     = Column(Boolean, default=False)
    created_at   = Column(DateTime, default=lambda: datetime.now(timezone.utc))


def init_db():
    """Create all tables and seed a demo API key on first run."""
    Base.metadata.create_all(bind=engine)

    db = SessionLocal()
    try:
        # Seed a demo key so the dashboard can query immediately
        existing = db.query(ApiKey).filter(ApiKey.key == "ap_live_demo_key_00001").first()
        if not existing:
            db.add(ApiKey(
                key="ap_live_demo_key_00001",
                team_name="Demo Team",
                plan="pro"
            ))
            db.commit()
    finally:
        db.close()


def get_db():
    """FastAPI dependency: yields a DB session per request."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

