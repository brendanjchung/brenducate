"""
Brenducate API — Pydantic Request / Response Schemas

Focused strictly on Per-Tenant Margin Guardrails, Same-Family Smart Degradation,
and Sub-Millisecond Runaway Loop Interception.
"""

from datetime import datetime
from typing import Dict, List, Optional
from pydantic import BaseModel, Field


# ── Auth & Account Management ────────────────────────────────────────────────

class SignupRequest(BaseModel):
    team_name: str = Field(..., min_length=1, max_length=120)
    email:     str = Field(..., min_length=3, max_length=200)
    password:  str = Field(..., min_length=6, max_length=200)


class LoginRequest(BaseModel):
    email:    str
    password: str


class AuthResponse(BaseModel):
    account_id:     str
    team_name:      str
    email:          str
    plan:           str
    api_key_prefix: str
    session_token:  str
    api_key:        Optional[str] = None  # Returned in full only on signup/rotation


class AccountProfile(BaseModel):
    account_id:        str
    team_name:         str
    email:             str
    plan:              str
    api_key_prefix:    str
    slack_webhook_url: Optional[str] = None
    created_at:        datetime


class RotateKeyResponse(BaseModel):
    api_key:        str
    api_key_prefix: str


class WebhookUpdateRequest(BaseModel):
    slack_webhook_url: Optional[str] = None


# ── Policies & Smart Degradation ─────────────────────────────────────────────

class GuardrailPolicyPayload(BaseModel):
    degrade_after_run_cost_usd: float = Field(0.25, ge=0.0)
    max_run_cost_usd:           float = Field(1.50, ge=0.01)
    tenant_daily_degrade_usd:   float = Field(5.00, ge=0.0)
    tenant_daily_cap_usd:       float = Field(25.00, ge=0.01)
    max_hops:                   int   = Field(12, ge=1, le=200)
    kill_on_infinite_loop:      bool  = True
    max_duplicate_calls:        int   = Field(2, ge=1, le=20)
    model_fallback_map:         Dict[str, str] = Field(default_factory=dict)


class TenantSyncState(BaseModel):
    spent_today_usd: float
    status:          str   # ok | degrade | blocked
    override_mode:   str   # auto | force_degrade | blocked


class PolicySyncResponse(BaseModel):
    policy:      GuardrailPolicyPayload
    period_date: str
    tenants:     Dict[str, TenantSyncState]


class TenantModeUpdateRequest(BaseModel):
    override_mode: str = Field(..., pattern="^(auto|force_degrade|blocked)$")


# ── Inbound Trace Ingestion (SDK → API) ──────────────────────────────────────

class TraceStep(BaseModel):
    step_index: int
    step_type:  str    # thought | tool | degrade | guardrail | final
    title:      str
    content:    str
    tool_call:  Optional[str] = None


class IngestTraceRequest(BaseModel):
    tenant_id:             str = "default"
    agent_id:              str
    original_model:        Optional[str] = None
    executed_model:        Optional[str] = None
    model:                 Optional[str] = None  # Fallback field if legacy callers send `model`
    was_degraded:          bool = False
    prompt:                str = "(no prompt captured)"
    status:                str = "COMPLETED"  # COMPLETED | DEGRADED | CIRCUIT_KILLED | BUDGET_BLOCKED | ERROR
    input_tokens:          int = Field(0, ge=0)
    cached_input_tokens:   int = Field(0, ge=0)
    cache_creation_tokens: int = Field(0, ge=0)
    output_tokens:         int = Field(0, ge=0)
    reasoning_tokens:      int = Field(0, ge=0)
    cost_usd:              float = Field(0.0, ge=0.0)
    estimated_savings_usd: float = Field(0.0, ge=0.0)
    latency_ms:            int = Field(0, ge=0)
    hops:                  int = Field(1, ge=0)
    circuit_tripped:       bool = False
    guardrail_reason:      Optional[str] = None
    steps:                 Optional[List[TraceStep]] = []
    environment:           str = "production"


class BatchIngestRequest(BaseModel):
    traces: List[IngestTraceRequest] = Field(..., max_length=100)


# ── Outbound Dashboard Views (API → UI) ──────────────────────────────────────

class TraceOut(BaseModel):
    id:                    str
    tenant_id:             str
    agent_id:              str
    original_model:        str
    executed_model:        str
    was_degraded:          bool
    prompt:                str
    status:                str
    input_tokens:          int
    cached_input_tokens:   int
    cache_creation_tokens: int
    output_tokens:         int
    reasoning_tokens:      int
    total_tokens:          int
    cost_usd:              float
    estimated_savings_usd: float
    latency_ms:            int
    hops:                  int
    circuit_tripped:       bool
    guardrail_reason:      Optional[str]
    environment:           str
    created_at:            datetime

    model_config = {"from_attributes": True}


class AlertOut(BaseModel):
    id:         int
    tenant_id:  str
    alert_type: str
    agent_id:   str
    message:    str
    severity:   str
    resolved:   bool
    created_at: datetime

    model_config = {"from_attributes": True}


class TenantMarginRow(BaseModel):
    tenant_id:            str
    spent_today_usd:      float
    saved_today_usd:      float
    spent_30d_usd:        float
    saved_30d_usd:        float
    run_count:            int
    degraded_calls_count: int
    killed_runs_count:    int
    override_mode:        str
    status:               str   # ok | degrade | blocked


class DashboardMarginsResponse(BaseModel):
    net_margin_saved_30d_usd: float
    total_spend_24h_usd:      float
    total_spend_30d_usd:      float
    total_runs_24h:           int
    degraded_calls_24h:       int
    killed_runs_24h:          int
    active_tenants_count:     int
    avg_latency_ms:           float
    open_alerts:              int
    top_tenants:              List[TenantMarginRow]


# ── Billing (Stripe) ─────────────────────────────────────────────────────────

class CreateCheckoutSessionRequest(BaseModel):
    plan:        str = "pro"       # free | pro | scale
    email:       str
    team_name:   str
    success_url: Optional[str] = None
    cancel_url:  Optional[str] = None


class CheckoutSessionResponse(BaseModel):
    checkout_url: str
    session_id:   str
    plan:         str
