"""
Brenducate API — FastAPI Backend for Per-Tenant Margin Guardrails & Smart Degradation

Core Routes:
  POST  /v1/auth/signup                 — create account, default policy, & API key
  POST  /v1/auth/login                  — authenticate dashboard session
  GET   /v1/auth/me                     — current workspace profile
  POST  /v1/auth/keys/rotate            — rotate ingestion API key
  PATCH /v1/auth/webhook                — configure Slack/webhook URL
  GET   /v1/policies/sync               — SDK background sync (policies + tenant states)
  PUT   /v1/policies                    — update per-run & per-tenant budget rules
  POST  /v1/telemetry/traces            — ingest single trace
  POST  /v1/telemetry/traces/batch      — ingest batched traces from SDK queue
  GET   /v1/telemetry/traces            — list workspace traces (tenant/status filters)
  GET   /v1/telemetry/traces/{id}       — single trace with step graph
  GET   /v1/dashboard/margins           — unit-economics KPIs & top burning tenants
  PATCH /v1/tenants/{tenant_id}/mode    — manual force_degrade / block / auto override
  GET   /v1/alerts                      — active guardrail alerts
  PATCH /v1/alerts/{alert_id}/resolve   — mark alert resolved
  POST  /v1/alerts/test-webhook         — send live Slack test ping
  POST  /v1/billing/create-checkout-session — Stripe Checkout
  POST  /v1/billing/webhook             — Stripe webhook handler
"""

import json
import os
import uuid
import secrets
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional

import httpx
import stripe
from dotenv import load_dotenv
from fastapi import BackgroundTasks, Depends, FastAPI, Header, HTTPException, Request, status
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import func
from sqlalchemy.orm import Session

from auth_utils import (
    create_session_token,
    generate_api_key,
    get_api_key_prefix,
    hash_api_key,
    hash_password,
    verify_password,
    verify_session_token,
)
from database import (
    Account,
    Alert,
    DEFAULT_MODEL_FALLBACK_MAP,
    GuardrailPolicy,
    TenantUsageRollup,
    Trace,
    engine,
    get_db,
    init_db,
)
from schemas import (
    AccountProfile,
    AlertOut,
    AuthResponse,
    BatchIngestRequest,
    CheckoutSessionResponse,
    CreateCheckoutSessionRequest,
    DashboardMarginsResponse,
    GuardrailPolicyPayload,
    IngestTraceRequest,
    LoginRequest,
    PolicySyncResponse,
    RotateKeyResponse,
    SignupRequest,
    TenantMarginRow,
    TenantModeUpdateRequest,
    TenantSyncState,
    TraceOut,
    WebhookUpdateRequest,
)

load_dotenv()

STRIPE_SECRET_KEY = os.getenv("STRIPE_SECRET_KEY", "")
STRIPE_WEBHOOK_SECRET = os.getenv("STRIPE_WEBHOOK_SECRET", "")
STRIPE_PAYMENT_LINK_PRO = os.getenv("STRIPE_PAYMENT_LINK_PRO", "")
STRIPE_PAYMENT_LINK_SCALE = os.getenv("STRIPE_PAYMENT_LINK_SCALE", "")
FRONTEND_URL = os.getenv(
    "FRONTEND_URL",
    "https://www.brenducate.com" if os.getenv("VERCEL") else "http://localhost:3032",
)

if STRIPE_SECRET_KEY:
    stripe.api_key = STRIPE_SECRET_KEY

PLANS_CONFIG = {
    "free": {
        "name": "Open Source / Free",
        "price_usd": 0,
        "runs_limit": 25_000,
        "price_id": os.getenv("STRIPE_PRICE_FREE", ""),
    },
    "pro": {
        "name": "Pro Margin Guard",
        "price_usd": 99,
        "runs_limit": 250_000,
        "price_id": os.getenv("STRIPE_PRICE_PRO", ""),
    },
    "scale": {
        "name": "Scale Fleet",
        "price_usd": 299,
        "runs_limit": 2_000_000,
        "price_id": os.getenv("STRIPE_PRICE_SCALE", ""),
    },
}


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    yield


app = FastAPI(
    title="Brenducate API",
    description="Per-tenant margin guardrails, same-family model step-down, and sub-millisecond loop interception.",
    version="0.2.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ── Helpers ──────────────────────────────────────────────────────────────────

def _today_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _compute_tenant_status(spent_today: float, override_mode: str, policy: GuardrailPolicy) -> str:
    if override_mode == "blocked" or spent_today >= policy.tenant_daily_cap_usd:
        return "blocked"
    if override_mode == "force_degrade" or spent_today >= policy.tenant_daily_degrade_usd:
        return "degrade"
    return "ok"


def _policy_to_payload(policy: GuardrailPolicy) -> GuardrailPolicyPayload:
    try:
        fallback_map = json.loads(policy.model_fallback_map_json or "{}")
    except Exception:
        fallback_map = dict(DEFAULT_MODEL_FALLBACK_MAP)
    if not fallback_map:
        fallback_map = dict(DEFAULT_MODEL_FALLBACK_MAP)
    return GuardrailPolicyPayload(
        degrade_after_run_cost_usd=policy.degrade_after_run_cost_usd,
        max_run_cost_usd=policy.max_run_cost_usd,
        tenant_daily_degrade_usd=policy.tenant_daily_degrade_usd,
        tenant_daily_cap_usd=policy.tenant_daily_cap_usd,
        max_hops=policy.max_hops,
        kill_on_infinite_loop=policy.kill_on_infinite_loop,
        max_duplicate_calls=policy.max_duplicate_calls,
        model_fallback_map=fallback_map,
    )


def _get_or_create_policy(db: Session, account_id: str) -> GuardrailPolicy:
    policy = db.query(GuardrailPolicy).filter(GuardrailPolicy.account_id == account_id).first()
    if not policy:
        policy = GuardrailPolicy(account_id=account_id)
        db.add(policy)
        db.commit()
        db.refresh(policy)
    return policy


def _dispatch_slack_webhook(webhook_url: str, title: str, fields: Dict[str, str]) -> bool:
    """Best-effort webhook delivery for Slack/Discord incoming webhooks."""
    if not webhook_url or not webhook_url.startswith(("http://", "https://")):
        return False
    lines = [f"*{title}*"] + [f"• *{k}:* {v}" for k, v in fields.items()]
    payload = {"text": "\n".join(lines)}
    try:
        with httpx.Client(timeout=4.0) as client:
            res = client.post(webhook_url, json=payload)
            return res.status_code < 300
    except Exception:
        return False


def get_current_account(
    authorization: Optional[str] = Header(None),
    db: Session = Depends(get_db),
) -> Account:
    """
    Authenticate requests via EITHER:
      1. Dashboard Session Token (`Bearer sess_...`)
      2. Production API Key (`Bearer br_live_...`)
    """
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing or invalid Authorization header. Expected Bearer token.",
        )
    raw = authorization.removeprefix("Bearer ").strip()

    if raw.startswith("sess_"):
        account_id = verify_session_token(raw)
        if not account_id:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid or expired session.")
        acc = db.query(Account).filter(Account.id == account_id, Account.active == True).first()
        if not acc:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Account inactive or not found.")
        return acc

    key_hash = hash_api_key(raw)
    acc = db.query(Account).filter(Account.api_key_hash == key_hash, Account.active == True).first()
    if not acc:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid or deactivated API key.")
    return acc


# ── Auth & Account Routes ────────────────────────────────────────────────────

@app.post("/v1/auth/signup", response_model=AuthResponse, status_code=201)
def signup(body: SignupRequest, db: Session = Depends(get_db)):
    email_norm = body.email.strip().lower()
    if db.query(Account).filter(Account.email == email_norm).first():
        raise HTTPException(status_code=409, detail="An account with this email already exists.")

    account_id = "acc_" + uuid.uuid4().hex[:12]
    raw_key = generate_api_key()
    acc = Account(
        id=account_id,
        email=email_norm,
        team_name=body.team_name.strip(),
        password_hash=hash_password(body.password),
        api_key_hash=hash_api_key(raw_key),
        api_key_prefix=get_api_key_prefix(raw_key),
        plan="free",
        active=True,
    )
    db.add(acc)
    db.flush()
    db.add(GuardrailPolicy(account_id=account_id))
    db.commit()
    db.refresh(acc)

    return AuthResponse(
        account_id=acc.id,
        team_name=acc.team_name,
        email=acc.email,
        plan=acc.plan,
        api_key_prefix=acc.api_key_prefix,
        session_token=create_session_token(acc.id),
        api_key=raw_key,
    )


@app.post("/v1/auth/login", response_model=AuthResponse)
def login(body: LoginRequest, db: Session = Depends(get_db)):
    email_norm = body.email.strip().lower()
    acc = db.query(Account).filter(Account.email == email_norm, Account.active == True).first()
    if not acc or not verify_password(body.password, acc.password_hash):
        raise HTTPException(status_code=401, detail="Invalid email or password.")
    return AuthResponse(
        account_id=acc.id,
        team_name=acc.team_name,
        email=acc.email,
        plan=acc.plan,
        api_key_prefix=acc.api_key_prefix,
        session_token=create_session_token(acc.id),
        api_key=None,
    )


@app.get("/v1/auth/me", response_model=AccountProfile)
def get_me(acc: Account = Depends(get_current_account)):
    return AccountProfile(
        account_id=acc.id,
        team_name=acc.team_name,
        email=acc.email,
        plan=acc.plan,
        api_key_prefix=acc.api_key_prefix,
        slack_webhook_url=acc.slack_webhook_url,
        created_at=acc.created_at,
    )


@app.post("/v1/auth/keys/rotate", response_model=RotateKeyResponse)
def rotate_api_key(
    acc: Account = Depends(get_current_account),
    db: Session = Depends(get_db),
):
    if acc.id == "acc_demo_sandbox":
        raise HTTPException(status_code=403, detail="Cannot rotate the shared demo sandbox key.")
    new_key = generate_api_key()
    acc.api_key_hash = hash_api_key(new_key)
    acc.api_key_prefix = get_api_key_prefix(new_key)
    db.commit()
    return RotateKeyResponse(api_key=new_key, api_key_prefix=acc.api_key_prefix)


@app.patch("/v1/auth/webhook")
def update_webhook(
    body: WebhookUpdateRequest,
    acc: Account = Depends(get_current_account),
    db: Session = Depends(get_db),
):
    url = (body.slack_webhook_url or "").strip() or None
    if url and not url.startswith(("https://", "http://")):
        raise HTTPException(status_code=400, detail="Webhook URL must start with https:// or http://")
    acc.slack_webhook_url = url
    db.commit()
    return {"status": "saved", "slack_webhook_url": acc.slack_webhook_url}


# ── Policies & SDK Sync Routes ───────────────────────────────────────────────

@app.get("/v1/policies/sync", response_model=PolicySyncResponse)
def sync_policies_for_sdk(
    acc: Account = Depends(get_current_account),
    db: Session = Depends(get_db),
):
    """
    Polled in the background by the Python SDK every 60s.
    Returns active guardrail limits + today's per-tenant spend and status.
    """
    policy = _get_or_create_policy(db, acc.id)
    today = _today_utc()
    rollups = db.query(TenantUsageRollup).filter(
        TenantUsageRollup.account_id == acc.id,
        TenantUsageRollup.period_date == today,
    ).all()

    tenants: Dict[str, TenantSyncState] = {}
    for row in rollups:
        tenants[row.tenant_id] = TenantSyncState(
            spent_today_usd=round(row.total_cost_usd, 6),
            status=_compute_tenant_status(row.total_cost_usd, row.override_mode, policy),
            override_mode=row.override_mode,
        )

    return PolicySyncResponse(
        policy=_policy_to_payload(policy),
        period_date=today,
        tenants=tenants,
    )


@app.put("/v1/policies", response_model=GuardrailPolicyPayload)
def update_policies(
    body: GuardrailPolicyPayload,
    acc: Account = Depends(get_current_account),
    db: Session = Depends(get_db),
):
    policy = _get_or_create_policy(db, acc.id)
    policy.degrade_after_run_cost_usd = body.degrade_after_run_cost_usd
    policy.max_run_cost_usd = body.max_run_cost_usd
    policy.tenant_daily_degrade_usd = body.tenant_daily_degrade_usd
    policy.tenant_daily_cap_usd = body.tenant_daily_cap_usd
    policy.max_hops = body.max_hops
    policy.kill_on_infinite_loop = body.kill_on_infinite_loop
    policy.max_duplicate_calls = body.max_duplicate_calls
    if body.model_fallback_map:
        policy.model_fallback_map_json = json.dumps(body.model_fallback_map)
    db.commit()
    db.refresh(policy)
    return _policy_to_payload(policy)


@app.patch("/v1/tenants/{tenant_id}/mode")
def update_tenant_mode(
    tenant_id: str,
    body: TenantModeUpdateRequest,
    acc: Account = Depends(get_current_account),
    db: Session = Depends(get_db),
):
    """Allow founders to manually force_degrade, block, or restore a tenant for today."""
    today = _today_utc()
    rollup = db.query(TenantUsageRollup).filter(
        TenantUsageRollup.account_id == acc.id,
        TenantUsageRollup.tenant_id == tenant_id,
        TenantUsageRollup.period_date == today,
    ).first()
    if not rollup:
        rollup = TenantUsageRollup(
            account_id=acc.id,
            tenant_id=tenant_id,
            period_date=today,
            override_mode=body.override_mode,
        )
        db.add(rollup)
    else:
        rollup.override_mode = body.override_mode
    db.commit()
    return {"tenant_id": tenant_id, "override_mode": body.override_mode, "period_date": today}


# ── Telemetry Ingestion ──────────────────────────────────────────────────────

def _persist_single_trace(
    db: Session,
    acc: Account,
    policy: GuardrailPolicy,
    body: IngestTraceRequest,
    background_tasks: BackgroundTasks,
) -> Trace:
    trace_id = "TR-" + uuid.uuid4().hex[:8].upper()
    orig_model = body.original_model or body.model or "gpt-4o"
    exec_model = body.executed_model or orig_model
    was_degraded = body.was_degraded or (orig_model != exec_model)
    total_tokens = (
        body.input_tokens
        + body.cached_input_tokens
        + body.cache_creation_tokens
        + body.output_tokens
    )
    steps_json = json.dumps([s.model_dump() for s in body.steps]) if body.steps else "[]"
    tenant_id = (body.tenant_id or "default").strip() or "default"

    trace = Trace(
        id=trace_id,
        account_id=acc.id,
        tenant_id=tenant_id,
        agent_id=body.agent_id,
        original_model=orig_model,
        executed_model=exec_model,
        was_degraded=was_degraded,
        prompt=(body.prompt or "")[:500],
        status=body.status,
        input_tokens=body.input_tokens,
        cached_input_tokens=body.cached_input_tokens,
        cache_creation_tokens=body.cache_creation_tokens,
        output_tokens=body.output_tokens,
        reasoning_tokens=body.reasoning_tokens,
        total_tokens=total_tokens,
        cost_usd=round(body.cost_usd, 6),
        estimated_savings_usd=round(body.estimated_savings_usd, 6),
        latency_ms=body.latency_ms,
        hops=body.hops,
        circuit_tripped=body.circuit_tripped,
        guardrail_reason=body.guardrail_reason,
        steps_json=steps_json,
        environment=body.environment,
    )
    db.add(trace)

    # Update today's tenant unit-economics rollup
    today = _today_utc()
    rollup = db.query(TenantUsageRollup).filter(
        TenantUsageRollup.account_id == acc.id,
        TenantUsageRollup.tenant_id == tenant_id,
        TenantUsageRollup.period_date == today,
    ).first()
    prev_spend = rollup.total_cost_usd if rollup else 0.0
    if not rollup:
        rollup = TenantUsageRollup(
            account_id=acc.id,
            tenant_id=tenant_id,
            period_date=today,
            total_cost_usd=0.0,
            saved_cost_usd=0.0,
            run_count=0,
            degraded_calls_count=0,
            killed_runs_count=0,
            input_tokens=0,
            cached_input_tokens=0,
            output_tokens=0,
            reasoning_tokens=0,
        )
        db.add(rollup)

    rollup.total_cost_usd = round((rollup.total_cost_usd or 0.0) + body.cost_usd, 6)
    rollup.saved_cost_usd = round((rollup.saved_cost_usd or 0.0) + body.estimated_savings_usd, 6)
    rollup.run_count = (rollup.run_count or 0) + 1
    if was_degraded:
        rollup.degraded_calls_count = (rollup.degraded_calls_count or 0) + 1
    if body.circuit_tripped or body.status == "BUDGET_BLOCKED":
        rollup.killed_runs_count = (rollup.killed_runs_count or 0) + 1
    rollup.input_tokens = (rollup.input_tokens or 0) + body.input_tokens
    rollup.cached_input_tokens = (rollup.cached_input_tokens or 0) + body.cached_input_tokens
    rollup.output_tokens = (rollup.output_tokens or 0) + body.output_tokens
    rollup.reasoning_tokens = (rollup.reasoning_tokens or 0) + body.reasoning_tokens
    # Flush so subsequent traces for the same tenant in a batch see this row
    db.flush()

    # Create actionable alerts & dispatch webhook on threshold/loop events
    new_alert: Optional[Alert] = None
    if body.status == "BUDGET_BLOCKED":
        new_alert = Alert(
            account_id=acc.id,
            tenant_id=tenant_id,
            alert_type="TENANT_CAP_EXCEEDED",
            agent_id=body.agent_id,
            message=body.guardrail_reason or f"Tenant '{tenant_id}' blocked after breaching daily cap.",
            severity="critical",
        )
    elif body.circuit_tripped and body.guardrail_reason:
        new_alert = Alert(
            account_id=acc.id,
            tenant_id=tenant_id,
            alert_type="CIRCUIT_BREAKER",
            agent_id=body.agent_id,
            message=f"[{tenant_id}] Agent '{body.agent_id}' halted: {body.guardrail_reason}",
            severity="critical",
        )
    elif prev_spend < policy.tenant_daily_degrade_usd <= rollup.total_cost_usd:
        new_alert = Alert(
            account_id=acc.id,
            tenant_id=tenant_id,
            alert_type="SMART_DEGRADATION",
            agent_id=body.agent_id,
            message=(
                f"[{tenant_id}] Crossed ${policy.tenant_daily_degrade_usd:.2f}/day soft cap. "
                f"Auto-stepping down to cheaper same-family models."
            ),
            severity="warning",
        )

    if new_alert is not None:
        db.add(new_alert)
        if acc.slack_webhook_url:
            background_tasks.add_task(
                _dispatch_slack_webhook,
                acc.slack_webhook_url,
                f"Brenducate Guardrail · {new_alert.alert_type}",
                {
                    "Tenant": tenant_id,
                    "Agent": body.agent_id,
                    "Detail": new_alert.message,
                    "Saved This Run": f"${body.estimated_savings_usd:.4f}",
                },
            )

    return trace


@app.post("/v1/telemetry/traces", status_code=202)
def ingest_trace(
    body: IngestTraceRequest,
    background_tasks: BackgroundTasks,
    acc: Account = Depends(get_current_account),
    db: Session = Depends(get_db),
):
    policy = _get_or_create_policy(db, acc.id)
    trace = _persist_single_trace(db, acc, policy, body, background_tasks)
    db.commit()
    return {
        "trace_id": trace.id,
        "status": "accepted",
        "tenant_id": trace.tenant_id,
        "cost_usd": trace.cost_usd,
        "estimated_savings_usd": trace.estimated_savings_usd,
    }


@app.post("/v1/telemetry/traces/batch", status_code=202)
def ingest_trace_batch(
    body: BatchIngestRequest,
    background_tasks: BackgroundTasks,
    acc: Account = Depends(get_current_account),
    db: Session = Depends(get_db),
):
    policy = _get_or_create_policy(db, acc.id)
    ids = []
    for item in body.traces:
        trace = _persist_single_trace(db, acc, policy, item, background_tasks)
        ids.append(trace.id)
    db.commit()
    return {"accepted": len(ids), "trace_ids": ids}


# ── Dashboard Queries (Traces, Tenant Margins, Alerts) ───────────────────────

@app.get("/v1/telemetry/traces", response_model=List[TraceOut])
def list_traces(
    limit: int = 50,
    tenant_id: Optional[str] = None,
    agent_id: Optional[str] = None,
    status_filter: Optional[str] = None,
    acc: Account = Depends(get_current_account),
    db: Session = Depends(get_db),
):
    q = db.query(Trace).filter(Trace.account_id == acc.id)
    if tenant_id:
        q = q.filter(Trace.tenant_id == tenant_id)
    if agent_id:
        q = q.filter(Trace.agent_id == agent_id)
    if status_filter:
        q = q.filter(Trace.status == status_filter)
    return q.order_by(Trace.created_at.desc()).limit(min(limit, 200)).all()


@app.get("/v1/telemetry/traces/{trace_id}")
def get_trace(
    trace_id: str,
    acc: Account = Depends(get_current_account),
    db: Session = Depends(get_db),
):
    trace = db.query(Trace).filter(Trace.id == trace_id, Trace.account_id == acc.id).first()
    if not trace:
        raise HTTPException(status_code=404, detail="Trace not found.")
    steps = json.loads(trace.steps_json) if trace.steps_json else []
    return {
        "id": trace.id,
        "tenant_id": trace.tenant_id,
        "agent_id": trace.agent_id,
        "original_model": trace.original_model,
        "executed_model": trace.executed_model,
        "was_degraded": trace.was_degraded,
        "prompt": trace.prompt,
        "status": trace.status,
        "input_tokens": trace.input_tokens,
        "cached_input_tokens": trace.cached_input_tokens,
        "cache_creation_tokens": trace.cache_creation_tokens,
        "output_tokens": trace.output_tokens,
        "reasoning_tokens": trace.reasoning_tokens,
        "total_tokens": trace.total_tokens,
        "cost_usd": trace.cost_usd,
        "estimated_savings_usd": trace.estimated_savings_usd,
        "latency_ms": trace.latency_ms,
        "hops": trace.hops,
        "circuit_tripped": trace.circuit_tripped,
        "guardrail_reason": trace.guardrail_reason,
        "environment": trace.environment,
        "created_at": trace.created_at,
        "steps": steps,
    }


@app.get("/v1/dashboard/margins", response_model=DashboardMarginsResponse)
def get_dashboard_margins(
    acc: Account = Depends(get_current_account),
    db: Session = Depends(get_db),
):
    """Aggregate margin savings, burn across tenants, and interception metrics."""
    now = datetime.now(timezone.utc)
    cutoff_24h = now - timedelta(hours=24)
    cutoff_30d_date = (now - timedelta(days=30)).strftime("%Y-%m-%d")
    today = _today_utc()
    policy = _get_or_create_policy(db, acc.id)

    traces_24h = db.query(Trace).filter(Trace.account_id == acc.id, Trace.created_at >= cutoff_24h)
    total_runs_24h = traces_24h.count()
    total_spend_24h = (
        db.query(func.sum(Trace.cost_usd))
        .filter(Trace.account_id == acc.id, Trace.created_at >= cutoff_24h)
        .scalar()
        or 0.0
    )
    degraded_24h = traces_24h.filter(Trace.was_degraded == True).count()
    killed_24h = traces_24h.filter(
        (Trace.circuit_tripped == True) | (Trace.status == "BUDGET_BLOCKED")
    ).count()
    avg_latency = (
        db.query(func.avg(Trace.latency_ms))
        .filter(Trace.account_id == acc.id, Trace.created_at >= cutoff_24h)
        .scalar()
        or 0.0
    )

    rollups_30d = db.query(TenantUsageRollup).filter(
        TenantUsageRollup.account_id == acc.id,
        TenantUsageRollup.period_date >= cutoff_30d_date,
    ).all()

    agg: Dict[str, dict] = {}
    net_saved_30d = 0.0
    total_spend_30d = 0.0
    for r in rollups_30d:
        net_saved_30d += r.saved_cost_usd or 0.0
        total_spend_30d += r.total_cost_usd or 0.0
        entry = agg.setdefault(
            r.tenant_id,
            {
                "spent_today": 0.0,
                "saved_today": 0.0,
                "spent_30d": 0.0,
                "saved_30d": 0.0,
                "runs": 0,
                "degraded": 0,
                "killed": 0,
                "override_mode": "auto",
            },
        )
        entry["spent_30d"] += r.total_cost_usd or 0.0
        entry["saved_30d"] += r.saved_cost_usd or 0.0
        entry["runs"] += r.run_count or 0
        entry["degraded"] += r.degraded_calls_count or 0
        entry["killed"] += r.killed_runs_count or 0
        if r.period_date == today:
            entry["spent_today"] += r.total_cost_usd or 0.0
            entry["saved_today"] += r.saved_cost_usd or 0.0
            entry["override_mode"] = r.override_mode or "auto"

    top_tenants = [
        TenantMarginRow(
            tenant_id=tid,
            spent_today_usd=round(v["spent_today"], 4),
            saved_today_usd=round(v["saved_today"], 4),
            spent_30d_usd=round(v["spent_30d"], 4),
            saved_30d_usd=round(v["saved_30d"], 4),
            run_count=v["runs"],
            degraded_calls_count=v["degraded"],
            killed_runs_count=v["killed"],
            override_mode=v["override_mode"],
            status=_compute_tenant_status(v["spent_today"], v["override_mode"], policy),
        )
        for tid, v in sorted(agg.items(), key=lambda kv: kv[1]["spent_30d"], reverse=True)
    ][:25]

    open_alerts = db.query(Alert).filter(Alert.account_id == acc.id, Alert.resolved == False).count()

    return DashboardMarginsResponse(
        net_margin_saved_30d_usd=round(net_saved_30d, 4),
        total_spend_24h_usd=round(total_spend_24h, 4),
        total_spend_30d_usd=round(total_spend_30d, 4),
        total_runs_24h=total_runs_24h,
        degraded_calls_24h=degraded_24h,
        killed_runs_24h=killed_24h,
        active_tenants_count=len(agg),
        avg_latency_ms=round(avg_latency, 1),
        open_alerts=open_alerts,
        top_tenants=top_tenants,
    )


@app.get("/v1/alerts", response_model=List[AlertOut])
def list_alerts(
    resolved: bool = False,
    acc: Account = Depends(get_current_account),
    db: Session = Depends(get_db),
):
    return (
        db.query(Alert)
        .filter(Alert.account_id == acc.id, Alert.resolved == resolved)
        .order_by(Alert.created_at.desc())
        .limit(30)
        .all()
    )


@app.patch("/v1/alerts/{alert_id}/resolve")
def resolve_alert(
    alert_id: int,
    acc: Account = Depends(get_current_account),
    db: Session = Depends(get_db),
):
    alert = db.query(Alert).filter(Alert.id == alert_id, Alert.account_id == acc.id).first()
    if not alert:
        raise HTTPException(status_code=404, detail="Alert not found.")
    alert.resolved = True
    db.commit()
    return {"status": "resolved", "alert_id": alert_id}


@app.post("/v1/alerts/test-webhook")
def test_webhook(
    acc: Account = Depends(get_current_account),
):
    if not acc.slack_webhook_url:
        raise HTTPException(status_code=400, detail="No Slack/webhook URL configured for this workspace.")
    ok = _dispatch_slack_webhook(
        acc.slack_webhook_url,
        "Brenducate Test Alert · Connection Verified",
        {
            "Workspace": acc.team_name,
            "Status": "Smart Degradation & Loop Guardrails Armed",
        },
    )
    if not ok:
        raise HTTPException(status_code=502, detail="Webhook endpoint failed to respond with 2xx status.")
    return {"status": "delivered", "webhook_url": acc.slack_webhook_url}


# ── Billing Routes (Stripe) ──────────────────────────────────────────────────

@app.get("/v1/billing/plans")
def list_billing_plans():
    return PLANS_CONFIG


@app.post("/v1/billing/create-checkout-session", response_model=CheckoutSessionResponse)
def create_checkout_session(body: CreateCheckoutSessionRequest, db: Session = Depends(get_db)):
    plan_key = body.plan.lower()
    if plan_key not in ("pro", "scale"):
        plan_key = "pro"
    plan_meta = PLANS_CONFIG[plan_key]

    success_url = body.success_url or f"{FRONTEND_URL}/dashboard.html?session_id={{CHECKOUT_SESSION_ID}}&status=success"
    cancel_url = body.cancel_url or f"{FRONTEND_URL}/dashboard.html?status=cancelled"

    if STRIPE_SECRET_KEY:
        try:
            line_item = (
                {"price": plan_meta["price_id"], "quantity": 1}
                if plan_meta.get("price_id")
                else {
                    "price_data": {
                        "currency": "usd",
                        "product_data": {
                            "name": f"Brenducate {plan_meta['name']}",
                            "description": f"Per-tenant margin guardrails & smart model step-down up to {plan_meta['runs_limit']:,} runs/month.",
                        },
                        "unit_amount": int(plan_meta["price_usd"] * 100),
                        "recurring": {"interval": "month"},
                    },
                    "quantity": 1,
                }
            )
            session = stripe.checkout.Session.create(
                payment_method_types=["card"],
                mode="subscription",
                customer_email=body.email.strip().lower(),
                line_items=[line_item],
                subscription_data={
                    "trial_period_days": 14,
                    "metadata": {"team_name": body.team_name, "plan": plan_key},
                },
                metadata={
                    "team_name": body.team_name,
                    "plan": plan_key,
                    "email": body.email.strip().lower(),
                },
                success_url=success_url,
                cancel_url=cancel_url,
            )
            return CheckoutSessionResponse(checkout_url=session.url, session_id=session.id, plan=plan_key)
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Stripe error: {str(e)}")

    payment_link = STRIPE_PAYMENT_LINK_PRO if plan_key == "pro" else STRIPE_PAYMENT_LINK_SCALE
    if payment_link and not STRIPE_SECRET_KEY:
        return CheckoutSessionResponse(
            checkout_url=payment_link,
            session_id="pl_direct_" + secrets.token_hex(6),
            plan=plan_key,
        )

    if os.getenv("VERCEL"):
        raise HTTPException(
            status_code=503,
            detail="Self-serve Stripe checkout is activating — email brendan@brenducate.com to unlock Pro/Scale or claim a 14-day founder trial.",
        )

    # Local dev/test checkout fallback when running outside production
    email_norm = body.email.strip().lower()
    acc = db.query(Account).filter(Account.email == email_norm).first()
    if acc:
        acc.plan = plan_key
        acc.stripe_customer_id = "cus_simulated"
        acc.stripe_subscription_id = "sub_simulated"
        db.commit()
    return CheckoutSessionResponse(
        checkout_url=f"{FRONTEND_URL}/dashboard.html?activated_plan={plan_key}",
        session_id="cs_simulated_" + secrets.token_hex(8),
        plan=plan_key,
    )


@app.post("/v1/billing/webhook")
async def stripe_webhook(request: Request, db: Session = Depends(get_db)):
    payload = await request.body()
    sig_header = request.headers.get("stripe-signature")

    if STRIPE_WEBHOOK_SECRET and sig_header:
        try:
            event = stripe.Webhook.construct_event(payload, sig_header, STRIPE_WEBHOOK_SECRET)
        except Exception as e:
            raise HTTPException(status_code=400, detail=f"Invalid webhook signature: {str(e)}")
    else:
        try:
            event = json.loads(payload)
        except Exception:
            raise HTTPException(status_code=400, detail="Invalid JSON payload")

    event_type = event.get("type")
    if event_type == "checkout.session.completed":
        session = event.get("data", {}).get("object", {})
        metadata = session.get("metadata", {})
        email_norm = (session.get("customer_email") or metadata.get("email") or "").strip().lower()
        plan = metadata.get("plan") or "pro"
        acc = db.query(Account).filter(Account.email == email_norm).first()
        if acc:
            acc.plan = plan
            acc.stripe_customer_id = session.get("customer")
            acc.stripe_subscription_id = session.get("subscription")
            acc.active = True
            db.commit()
    elif event_type == "customer.subscription.deleted":
        sub_id = event.get("data", {}).get("object", {}).get("id")
        if sub_id:
            acc = db.query(Account).filter(Account.stripe_subscription_id == sub_id).first()
            if acc:
                acc.plan = "free"
                db.commit()

    return {"status": "success", "event_type": event_type}


@app.get("/health")
def health():
    return {
        "status": "ok",
        "service": "Brenducate API",
        "version": "0.2.0",
        "database": engine.dialect.name,
    }
