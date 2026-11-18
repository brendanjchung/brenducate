"""
brenducate — Per-Tenant Margin Guardrails, Same-Family Smart Degradation,
and Sub-Millisecond Loop Interception for Production AI Agents.

Quickstart:
    from openai import OpenAI
    from brenducate import Brenducate, CircuitBreaker, wrap_openai

    sentinel = Brenducate(api_key="br_live_...")
    client = wrap_openai(OpenAI(), pulse=sentinel)

    with sentinel.tenant("acme_corp"):
        resp = client.chat.completions.create(
            model="gpt-4o",
            messages=[{"role": "user", "content": "Summarize Q3 report"}]
        )
"""

from .circuit_breaker import (
    CircuitBreaker,
    CircuitBreakerException,
    ExecutionState,
    TenantBudgetExceeded,
)
from .client import BrenducateClient, PolicyStore
from .integrations import wrap_anthropic, wrap_openai
from .monitor import AgentPulse, Brenducate
from .pricing import (
    DEFAULT_DEGRADATION_MAP,
    MODEL_PRICING,
    TokenUsage,
    calculate_cost,
    calculate_degradation_savings,
    calculate_token_cost,
    resolve_step_down_model,
)

__version__ = "0.2.1"
__all__ = [
    "Brenducate",
    "AgentPulse",
    "BrenducateClient",
    "PolicyStore",
    "CircuitBreaker",
    "CircuitBreakerException",
    "TenantBudgetExceeded",
    "ExecutionState",
    "TokenUsage",
    "MODEL_PRICING",
    "DEFAULT_DEGRADATION_MAP",
    "calculate_cost",
    "calculate_token_cost",
    "calculate_degradation_savings",
    "resolve_step_down_model",
    "wrap_openai",
    "wrap_anthropic",
]
