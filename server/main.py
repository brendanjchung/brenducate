"""
AgentPulse Ops — FastAPI Ingestion & Dashboard Backend
MVP: One file, zero deployment complexity.

Routes:
  POST /v1/auth/keys              — provision a new API key
  POST /v1/telemetry/traces       — ingest a trace from the SDK
  GET  /v1/telemetry/traces       — fetch traces for dashboard
  GET  /v1/telemetry/traces/{id}  — fetch a single trace with full steps
  GET  /v1/dashboard/summary      — KPI aggregate for the metrics cards
  GET  /v1/alerts                 — fetch open alerts

Run with:
    uvicorn main:app --reload --port 8000
"""

import uuid
import json
import secrets
import os
from datetime import datetime, timezone, timedelta
from typing import Optional, List

from dotenv import load_dotenv
import stripe
from fastapi import FastAPI, Depends, HTTPException, Header, Request, status
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.orm import Session
from sqlalchemy import func

from database import get_db, init_db, ApiKey, Trace, Alert
from schemas import (
    IngestTraceRequest, TraceOut, AlertOut, DashboardSummary,
    ApiKeyCreateRequest, ApiKeyCreateResponse,
    CreateCheckoutSessionRequest, CheckoutSessionResponse
)

# Load environment configuration
load_dotenv()

STRIPE_SECRET_KEY = os.getenv("STRIPE_SECRET_KEY", "")
STRIPE_WEBHOOK_SECRET = os.getenv("STRIPE_WEBHOOK_SECRET", "")
FRONTEND_URL = os.getenv("FRONTEND_URL", "http://localhost:3032")

if STRIPE_SECRET_KEY:
    stripe.api_key = STRIPE_SECRET_KEY

# Plan catalog ($10k MRR tiers)
PLANS_CONFIG = {
    "starter": {
        "name": "Starter Sentinel",
        "price_usd": 49,
        "runs_limit": 50_000,
        "price_id": os.getenv("STRIPE_PRICE_STARTER", ""),
    },
    "pro": {
        "name": "Professional Ops",
        "price_usd": 99,
        "runs_limit": 500_000,
        "price_id": os.getenv("STRIPE_PRICE_PRO", ""),
    },
    "scale": {
        "name": "Scale Sentinel",
        "price_usd": 249,
        "runs_limit": 2_500_000,
        "price_id": os.getenv("STRIPE_PRICE_SCALE", ""),
    }
}

# ── App Bootstrap ────────────────────────────────────────────────────────────

app = FastAPI(
    title="AgentPulse Ops API",
    description="Real-time LLM agent telemetry, circuit-breaker guardrails, and prompt drift monitoring.",
    version="0.1.0",
)

# Allow the frontend dashboard (localhost:3031 or Vercel) to call the API
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],   # Tighten this to your domain before shipping
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.on_event("startup")
def on_startup():
    init_db()
    print("✅ AgentPulse backend ready. Database initialised.")


# ── Auth Helper ──────────────────────────────────────────────────────────────

def get_api_key(
    authorization: Optional[str] = Header(None),
    db: Session = Depends(get_db)
) -> ApiKey:
    """
    Extract and validate 'Authorization: Bearer ap_live_...' header.
    Returns the ApiKey ORM row if valid, raises 401 otherwise.
    """
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing or invalid Authorization header. Use: Bearer <your_api_key>"
        )
    raw_key = authorization.removeprefix("Bearer ").strip()
    key_row = db.query(ApiKey).filter(ApiKey.key == raw_key, ApiKey.active == True).first()
    if not key_row:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="API key not found or deactivated."
        )
    return key_row


# ── Route: Provision API Key ─────────────────────────────────────────────────

@app.post("/v1/auth/keys", response_model=ApiKeyCreateResponse, status_code=201)
def create_api_key(body: ApiKeyCreateRequest, db: Session = Depends(get_db)):
    """
    Generate a new API key for a team.
    In production, this is called automatically upon Stripe checkout.
    """
    new_key = "ap_live_" + secrets.token_hex(16)
    key_row = ApiKey(key=new_key, team_name=body.team_name, email=body.email, plan=body.plan)
    db.add(key_row)
    db.commit()
    db.refresh(key_row)
    return ApiKeyCreateResponse(key=key_row.key, team_name=key_row.team_name, plan=key_row.plan, email=key_row.email)


# ── Billing Routes (Stripe) ──────────────────────────────────────────────────

@app.get("/v1/billing/plans")
def list_billing_plans():
    """List available subscription tiers ($10k MRR architecture)."""
    return PLANS_CONFIG


@app.post("/v1/billing/create-checkout-session", response_model=CheckoutSessionResponse)
def create_checkout_session(body: CreateCheckoutSessionRequest, db: Session = Depends(get_db)):
    """
    Create a Stripe Checkout Session for subscription tier upgrade.
    Includes 14-day free trial on recurring subscriptions.
    """
    plan_key = body.plan.lower()
    plan_meta = PLANS_CONFIG.get(plan_key, PLANS_CONFIG["pro"])

    success_url = body.success_url or f"{FRONTEND_URL}/index.html?session_id={{CHECKOUT_SESSION_ID}}&status=success"
    cancel_url = body.cancel_url or f"{FRONTEND_URL}/index.html?status=cancelled"

    # If Stripe API key is provided, generate a real live/test checkout session
    if STRIPE_SECRET_KEY:
        try:
            line_item = {}
            if plan_meta.get("price_id"):
                line_item = {"price": plan_meta["price_id"], "quantity": 1}
            else:
                line_item = {
                    "price_data": {
                        "currency": "usd",
                        "product_data": {
                            "name": f"AgentPulse {plan_meta['name']}",
                            "description": f"Observability, circuit-breaker guardrails, and drift sentinel for {plan_meta['runs_limit']:,} agent runs/month.",
                        },
                        "unit_amount": int(plan_meta["price_usd"] * 100),
                        "recurring": {"interval": "month"},
                    },
                    "quantity": 1,
                }

            session = stripe.checkout.Session.create(
                payment_method_types=["card"],
                mode="subscription",
                customer_email=body.email,
                line_items=[line_item],
                subscription_data={
                    "trial_period_days": 14,
                    "metadata": {
                        "team_name": body.team_name,
                        "plan": plan_key,
                    }
                },
                metadata={
                    "team_name": body.team_name,
                    "plan": plan_key,
                    "email": body.email,
                },
                success_url=success_url,
                cancel_url=cancel_url,
            )
            return CheckoutSessionResponse(
                checkout_url=session.url,
                session_id=session.id,
                plan=plan_key
            )
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Stripe session creation error: {str(e)}")

    # Sandbox / Demo Mode fallback (works out of the box before user enters Stripe keys)
    simulated_session_id = "cs_simulated_" + secrets.token_hex(12)
    new_key = "ap_live_" + secrets.token_hex(16)
    db.add(ApiKey(
        key=new_key,
        team_name=body.team_name,
        email=body.email,
        plan=plan_key,
        stripe_customer_id="cus_simulated",
        stripe_subscription_id="sub_simulated"
    ))
    db.commit()

    return CheckoutSessionResponse(
        checkout_url=f"{FRONTEND_URL}/index.html?activated_plan={plan_key}&key={new_key}",
        session_id=simulated_session_id,
        plan=plan_key
    )


@app.post("/v1/billing/webhook")
async def stripe_webhook(request: Request, db: Session = Depends(get_db)):
    """
    Handle incoming Stripe webhook events.
    Automatically provisions production API key upon successful checkout.
    """
    payload = await request.body()
    sig_header = request.headers.get("stripe-signature")

    event = None
    if STRIPE_WEBHOOK_SECRET and sig_header:
        try:
            event = stripe.Webhook.construct_event(payload, sig_header, STRIPE_WEBHOOK_SECRET)
        except Exception as e:
            raise HTTPException(status_code=400, detail=f"Invalid webhook signature: {str(e)}")
    else:
        # Development / untrusted mode parse
        try:
            event = json.loads(payload)
        except Exception:
            raise HTTPException(status_code=400, detail="Invalid JSON payload")

    event_type = event.get("type")

    # 1. New Paid Customer Activated
    if event_type == "checkout.session.completed":
        session = event.get("data", {}).get("object", {})
        metadata = session.get("metadata", {})
        customer_email = session.get("customer_email") or metadata.get("email") or "customer@agentpulse.io"
        team_name = metadata.get("team_name") or "New Team"
        plan = metadata.get("plan") or "pro"
        stripe_sub_id = session.get("subscription")
        stripe_cus_id = session.get("customer")

        # Provision live production key
        live_key = "ap_live_" + secrets.token_hex(16)
        db.add(ApiKey(
            key=live_key,
            team_name=team_name,
            email=customer_email,
            plan=plan,
            stripe_customer_id=stripe_cus_id,
            stripe_subscription_id=stripe_sub_id,
            active=True
        ))
        db.commit()
        print(f"🎉 Paid customer activated: {team_name} ({customer_email}) on plan '{plan}'. Key: {live_key}")

    # 2. Subscription Cancelled / Churned
    elif event_type == "customer.subscription.deleted":
        sub = event.get("data", {}).get("object", {})
        sub_id = sub.get("id")
        if sub_id:
            key_row = db.query(ApiKey).filter(ApiKey.stripe_subscription_id == sub_id).first()
            if key_row:
                key_row.active = False
                db.commit()
                print(f"⚠️ Deactivated API key for cancelled subscription: {sub_id}")

    return {"status": "success", "event_type": event_type}


# ── Route: Ingest Trace ──────────────────────────────────────────────────────

@app.post("/v1/telemetry/traces", status_code=202)
def ingest_trace(
    body: IngestTraceRequest,
    auth: ApiKey = Depends(get_api_key),
    db: Session = Depends(get_db)
):
    """
    Primary ingestion endpoint. Called by the AgentPulse SDK decorator
    at the end of every agent execution, with microsecond latency impact.
    """
    trace_id = "TR-" + str(uuid.uuid4())[:8].upper()
    total_tokens = body.input_tokens + body.output_tokens

    steps_json = json.dumps(
        [s.model_dump() for s in body.steps]
    ) if body.steps else "[]"

    trace = Trace(
        id=trace_id,
        api_key=auth.key,
        agent_id=body.agent_id,
        model=body.model,
        prompt=body.prompt[:500],   # Truncate long prompts for storage
        status=body.status,
        input_tokens=body.input_tokens,
        output_tokens=body.output_tokens,
        total_tokens=total_tokens,
        cost_usd=body.cost_usd,
        latency_ms=body.latency_ms,
        hops=body.hops,
        circuit_tripped=body.circuit_tripped,
        guardrail_reason=body.guardrail_reason,
        steps_json=steps_json,
        environment=body.environment,
    )
    db.add(trace)

    # Auto-create an alert if the circuit breaker fired
    if body.circuit_tripped and body.guardrail_reason:
        alert = Alert(
            api_key=auth.key,
            alert_type="CIRCUIT_BREAKER",
            agent_id=body.agent_id,
            message=f"Circuit breaker tripped on agent '{body.agent_id}': {body.guardrail_reason}",
            severity="critical",
        )
        db.add(alert)

    db.commit()

    return {
        "trace_id": trace_id,
        "status": "accepted",
        "message": f"Trace ingested for agent '{body.agent_id}' ({total_tokens:,} tokens, ${body.cost_usd:.4f})."
    }


# ── Route: List Traces ───────────────────────────────────────────────────────

@app.get("/v1/telemetry/traces", response_model=List[TraceOut])
def list_traces(
    limit: int = 50,
    agent_id: Optional[str] = None,
    status: Optional[str] = None,
    auth: ApiKey = Depends(get_api_key),
    db: Session = Depends(get_db)
):
    """
    Returns the latest traces for this team's API key.
    Filters: agent_id, status (COMPLETED | CIRCUIT_KILLED | DRIFT_WARNING).
    """
    q = db.query(Trace).filter(Trace.api_key == auth.key)
    if agent_id:
        q = q.filter(Trace.agent_id == agent_id)
    if status:
        q = q.filter(Trace.status == status)
    traces = q.order_by(Trace.created_at.desc()).limit(limit).all()
    return traces


# ── Route: Get Single Trace (with full steps) ────────────────────────────────

@app.get("/v1/telemetry/traces/{trace_id}")
def get_trace(
    trace_id: str,
    auth: ApiKey = Depends(get_api_key),
    db: Session = Depends(get_db)
):
    """Returns a single trace including full step-by-step execution graph."""
    trace = db.query(Trace).filter(
        Trace.id == trace_id, Trace.api_key == auth.key
    ).first()
    if not trace:
        raise HTTPException(status_code=404, detail="Trace not found.")

    steps = json.loads(trace.steps_json) if trace.steps_json else []
    return {
        "id": trace.id,
        "agent_id": trace.agent_id,
        "model": trace.model,
        "prompt": trace.prompt,
        "status": trace.status,
        "total_tokens": trace.total_tokens,
        "cost_usd": trace.cost_usd,
        "latency_ms": trace.latency_ms,
        "hops": trace.hops,
        "circuit_tripped": trace.circuit_tripped,
        "guardrail_reason": trace.guardrail_reason,
        "environment": trace.environment,
        "created_at": trace.created_at,
        "steps": steps,
    }


# ── Route: Dashboard KPI Summary ─────────────────────────────────────────────

@app.get("/v1/dashboard/summary", response_model=DashboardSummary)
def get_dashboard_summary(
    auth: ApiKey = Depends(get_api_key),
    db: Session = Depends(get_db)
):
    """Aggregates KPI metrics for the dashboard cards (last 24 hours)."""
    cutoff = datetime.now(timezone.utc) - timedelta(hours=24)

    base_q = db.query(Trace).filter(
        Trace.api_key == auth.key,
        Trace.created_at >= cutoff
    )

    total_traces  = base_q.count()
    total_tokens  = db.query(func.sum(Trace.total_tokens)).filter(
        Trace.api_key == auth.key, Trace.created_at >= cutoff
    ).scalar() or 0
    total_cost    = db.query(func.sum(Trace.cost_usd)).filter(
        Trace.api_key == auth.key, Trace.created_at >= cutoff
    ).scalar() or 0.0
    circuit_trips = base_q.filter(Trace.circuit_tripped == True).count()
    avg_latency   = db.query(func.avg(Trace.latency_ms)).filter(
        Trace.api_key == auth.key, Trace.created_at >= cutoff
    ).scalar() or 0.0

    # Count distinct active agents from traces in last 24h
    active_agents = db.query(func.count(func.distinct(Trace.agent_id))).filter(
        Trace.api_key == auth.key, Trace.created_at >= cutoff
    ).scalar() or 0

    open_alerts = db.query(Alert).filter(
        Alert.api_key == auth.key, Alert.resolved == False
    ).count()

    return DashboardSummary(
        active_agents=active_agents,
        total_traces_24h=total_traces,
        total_tokens_24h=total_tokens,
        total_cost_usd_24h=round(total_cost, 4),
        circuit_trips_today=circuit_trips,
        avg_latency_ms=round(avg_latency, 1),
        open_alerts=open_alerts,
    )


# ── Route: Alerts ────────────────────────────────────────────────────────────

@app.get("/v1/alerts", response_model=List[AlertOut])
def list_alerts(
    resolved: bool = False,
    auth: ApiKey = Depends(get_api_key),
    db: Session = Depends(get_db)
):
    """Returns open (or resolved) alerts for this team."""
    alerts = db.query(Alert).filter(
        Alert.api_key == auth.key,
        Alert.resolved == resolved
    ).order_by(Alert.created_at.desc()).limit(20).all()
    return alerts


@app.patch("/v1/alerts/{alert_id}/resolve")
def resolve_alert(
    alert_id: int,
    auth: ApiKey = Depends(get_api_key),
    db: Session = Depends(get_db)
):
    """Marks an alert as resolved."""
    alert = db.query(Alert).filter(
        Alert.id == alert_id, Alert.api_key == auth.key
    ).first()
    if not alert:
        raise HTTPException(status_code=404, detail="Alert not found.")
    alert.resolved = True
    db.commit()
    return {"status": "resolved", "alert_id": alert_id}


# ── Health Check ─────────────────────────────────────────────────────────────

@app.get("/health")
def health():
    return {"status": "ok", "service": "AgentPulse Ops API", "version": "0.1.0"}

