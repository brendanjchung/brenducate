"""
Brenducate — Multi-Tenant Database Layer (Postgres / SQLite)

Supports free-tier Neon / Supabase / Render PostgreSQL via DATABASE_URL
and zero-config SQLite for local development.
"""

import json
import os
import threading
from datetime import datetime, timezone
from sqlalchemy import (
    create_engine,
    Column,
    String,
    Float,
    Integer,
    Boolean,
    DateTime,
    Text,
    ForeignKey,
    UniqueConstraint,
)
from sqlalchemy.orm import declarative_base, sessionmaker

from auth_utils import hash_api_key, get_api_key_prefix

DEFAULT_MODEL_FALLBACK_MAP = {
    # Anthropic same-family step-downs (preserves Anthropic client & cache_control structure)
    "claude-opus-4": "claude-sonnet-4-20250514",
    "claude-sonnet-4": "claude-3-5-haiku-latest",
    "claude-3-7-sonnet": "claude-3-5-haiku-latest",
    "claude-3-5-sonnet": "claude-3-5-haiku-latest",
    # OpenAI same-family step-downs (preserves OpenAI client & schema formatting)
    "gpt-4.1": "gpt-4.1-mini",
    "gpt-4o": "gpt-4o-mini",
    "o1": "o3-mini",
    "o3": "o3-mini",
}

DEFAULT_FALLBACK_MAP_JSON = json.dumps(DEFAULT_MODEL_FALLBACK_MAP)

default_db = "sqlite:////tmp/brenducate.db" if os.getenv("VERCEL") else "sqlite:///./brenducate.db"
DATABASE_URL = os.getenv("DATABASE_URL") or os.getenv("POSTGRES_URL") or default_db

if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

if os.getenv("VERCEL") and DATABASE_URL.startswith("sqlite"):
    print("⚠️  [Brenducate] Running with ephemeral /tmp SQLite on Vercel. Set DATABASE_URL to Neon/Supabase Postgres for persistence.")

is_sqlite = DATABASE_URL.startswith("sqlite")
connect_args = {"check_same_thread": False} if is_sqlite else {}
engine_kwargs = {"connect_args": connect_args}
if not is_sqlite:
    # Crucial for serverless Postgres (Neon/Supabase) idle connection recycling
    engine_kwargs["pool_pre_ping"] = True
    engine_kwargs["pool_recycle"] = 300

engine = create_engine(DATABASE_URL, **engine_kwargs)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()

_init_lock = threading.Lock()
_db_ready = False


# ── ORM Models ───────────────────────────────────────────────────────────────

class Account(Base):
    __tablename__ = "accounts"

    id                     = Column(String, primary_key=True, index=True)
    email                  = Column(String, unique=True, index=True, nullable=False)
    team_name              = Column(String, nullable=False)
    password_hash          = Column(String, nullable=True)
    api_key_hash           = Column(String, unique=True, index=True, nullable=False)
    api_key_prefix         = Column(String, nullable=False)
    plan                   = Column(String, default="free")   # free | pro | scale
    stripe_customer_id     = Column(String, nullable=True)
    stripe_subscription_id = Column(String, nullable=True)
    slack_webhook_url      = Column(String, nullable=True)
    created_at             = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    active                 = Column(Boolean, default=True)


class GuardrailPolicy(Base):
    __tablename__ = "guardrail_policies"

    id                         = Column(Integer, primary_key=True, autoincrement=True)
    account_id                 = Column(String, ForeignKey("accounts.id", ondelete="CASCADE"), unique=True, index=True, nullable=False)
    degrade_after_run_cost_usd = Column(Float, default=0.25)
    max_run_cost_usd           = Column(Float, default=1.50)
    tenant_daily_degrade_usd   = Column(Float, default=5.00)
    tenant_daily_cap_usd       = Column(Float, default=25.00)
    max_hops                   = Column(Integer, default=12)
    kill_on_infinite_loop      = Column(Boolean, default=True)
    max_duplicate_calls        = Column(Integer, default=2)
    model_fallback_map_json    = Column(Text, default=DEFAULT_FALLBACK_MAP_JSON)
    updated_at                 = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))


class TenantUsageRollup(Base):
    __tablename__ = "tenant_usage_rollups"

    id                   = Column(Integer, primary_key=True, autoincrement=True)
    account_id           = Column(String, ForeignKey("accounts.id", ondelete="CASCADE"), index=True, nullable=False)
    tenant_id            = Column(String, index=True, nullable=False)
    period_date          = Column(String, index=True, nullable=False)  # YYYY-MM-DD (UTC)
    total_cost_usd       = Column(Float, default=0.0)
    saved_cost_usd       = Column(Float, default=0.0)
    run_count            = Column(Integer, default=0)
    degraded_calls_count = Column(Integer, default=0)
    killed_runs_count    = Column(Integer, default=0)
    input_tokens         = Column(Integer, default=0)
    cached_input_tokens  = Column(Integer, default=0)
    output_tokens        = Column(Integer, default=0)
    reasoning_tokens     = Column(Integer, default=0)
    override_mode        = Column(String, default="auto")  # auto | force_degrade | blocked
    updated_at           = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))

    __table_args__ = (
        UniqueConstraint("account_id", "tenant_id", "period_date", name="uq_account_tenant_date"),
    )


class Trace(Base):
    __tablename__ = "traces"

    id                    = Column(String, primary_key=True, index=True)
    account_id            = Column(String, ForeignKey("accounts.id", ondelete="CASCADE"), index=True, nullable=False)
    tenant_id             = Column(String, index=True, default="default")
    agent_id              = Column(String, index=True, nullable=False)
    original_model        = Column(String, nullable=False)
    executed_model        = Column(String, nullable=False)
    was_degraded          = Column(Boolean, default=False)
    prompt                = Column(Text, nullable=False)
    status                = Column(String, default="COMPLETED")  # COMPLETED | DEGRADED | CIRCUIT_KILLED | BUDGET_BLOCKED | ERROR
    input_tokens          = Column(Integer, default=0)
    cached_input_tokens   = Column(Integer, default=0)
    cache_creation_tokens = Column(Integer, default=0)
    output_tokens         = Column(Integer, default=0)
    reasoning_tokens      = Column(Integer, default=0)
    total_tokens          = Column(Integer, default=0)
    cost_usd              = Column(Float, default=0.0)
    estimated_savings_usd = Column(Float, default=0.0)
    latency_ms            = Column(Integer, default=0)
    hops                  = Column(Integer, default=1)
    circuit_tripped       = Column(Boolean, default=False)
    guardrail_reason      = Column(String, nullable=True)
    steps_json            = Column(Text, nullable=True)
    environment           = Column(String, default="production")
    created_at            = Column(DateTime(timezone=True), index=True, default=lambda: datetime.now(timezone.utc))


class Alert(Base):
    __tablename__ = "alerts"

    id         = Column(Integer, primary_key=True, autoincrement=True)
    account_id = Column(String, ForeignKey("accounts.id", ondelete="CASCADE"), index=True, nullable=False)
    tenant_id  = Column(String, default="default")
    alert_type = Column(String, nullable=False)   # CIRCUIT_BREAKER | SMART_DEGRADATION | TENANT_CAP_EXCEEDED
    agent_id   = Column(String, nullable=False)
    message    = Column(Text, nullable=False)
    severity   = Column(String, default="warning")  # info | warning | critical
    resolved   = Column(Boolean, default=False)
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))


def _seed_demo_account(db) -> None:
    """Ensure local demo sandbox account exists so quickstart examples work out of the box."""
    demo_raw_key = os.getenv("BRENDUCATE_DEMO_KEY", "br_live_demo_key_00001")
    demo_hash = hash_api_key(demo_raw_key)
    existing = db.query(Account).filter(Account.id == "acc_demo_sandbox").first()
    if not existing:
        existing = Account(
            id="acc_demo_sandbox",
            email="demo@brenducate.com",
            team_name="Demo Sandbox",
            password_hash=None,
            api_key_hash=demo_hash,
            api_key_prefix=get_api_key_prefix(demo_raw_key),
            plan="pro",
            active=True,
        )
        db.add(existing)
        db.flush()

    policy = db.query(GuardrailPolicy).filter(GuardrailPolicy.account_id == "acc_demo_sandbox").first()
    if not policy:
        db.add(GuardrailPolicy(account_id="acc_demo_sandbox"))

    db.commit()


def init_db(force: bool = False) -> None:
    """Create all tables idempotently and ensure default demo account exists."""
    global _db_ready
    with _init_lock:
        if _db_ready and not force:
            return
        Base.metadata.create_all(bind=engine)
        db = SessionLocal()
        try:
            _seed_demo_account(db)
            _db_ready = True
        finally:
            db.close()


def get_db():
    """FastAPI dependency: initializes DB lazily on cold start and yields session."""
    if not _db_ready:
        init_db()
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
