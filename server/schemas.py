"""
AgentPulse Ops — Pydantic Request / Response Schemas
"""

from pydantic import BaseModel, Field
from typing import Optional, List
from datetime import datetime


# ── Inbound (SDK → API) ──────────────────────────────────────────────────────

class TraceStep(BaseModel):
    step_index: int
    step_type:  str    # thought | tool | guardrail | final
    title:      str
    content:    str
    tool_call:  Optional[str] = None   # raw JSON string of the tool invocation


class IngestTraceRequest(BaseModel):
    agent_id:         str
    model:            str
    prompt:           str
    status:           str = "COMPLETED"    # COMPLETED | CIRCUIT_KILLED | DRIFT_WARNING
    input_tokens:     int = 0
    output_tokens:    int = 0
    cost_usd:         float = 0.0
    latency_ms:       int = 0
    hops:             int = 1
    circuit_tripped:  bool = False
    guardrail_reason: Optional[str] = None
    steps:            Optional[List[TraceStep]] = []
    environment:      str = "production"


# ── Outbound (API → Dashboard) ───────────────────────────────────────────────

class TraceOut(BaseModel):
    id:               str
    agent_id:         str
    model:            str
    prompt:           str
    status:           str
    total_tokens:     int
    cost_usd:         float
    latency_ms:       int
    hops:             int
    circuit_tripped:  bool
    guardrail_reason: Optional[str]
    environment:      str
    created_at:       datetime

    model_config = {"from_attributes": True}


class AlertOut(BaseModel):
    id:          int
    alert_type:  str
    agent_id:    str
    message:     str
    severity:    str
    resolved:    bool
    created_at:  datetime

    model_config = {"from_attributes": True}


class DashboardSummary(BaseModel):
    active_agents:         int
    total_traces_24h:      int
    total_tokens_24h:      int
    total_cost_usd_24h:    float
    circuit_trips_today:   int
    avg_latency_ms:        float
    open_alerts:           int


class ApiKeyCreateRequest(BaseModel):
    team_name: str
    plan:      str = "starter"
    email:     Optional[str] = None


class ApiKeyCreateResponse(BaseModel):
    key:       str
    team_name: str
    plan:      str
    email:     Optional[str] = None


class CreateCheckoutSessionRequest(BaseModel):
    plan:        str = "pro"       # starter | pro | scale
    email:       str
    team_name:   str
    success_url: Optional[str] = None
    cancel_url:  Optional[str] = None


class CheckoutSessionResponse(BaseModel):
    checkout_url: str
    session_id:   str
    plan:         str


