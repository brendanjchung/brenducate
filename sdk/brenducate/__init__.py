"""
brenducate — Production LLM Agent Observability & Circuit-Breaker SDK

Usage:
    from brenducate import Brenducate, CircuitBreaker, wrap_openai
"""

from .monitor import AgentPulse as Brenducate, AgentPulse
from .circuit_breaker import CircuitBreaker, CircuitBreakerException, ExecutionState
from .client import AgentPulseClient as BrenducateClient
from .integrations import wrap_openai, AgentPulseCallbackHandler as BrenducateCallbackHandler

__version__ = "0.1.0"
__all__ = [
    "Brenducate",
    "AgentPulse",
    "CircuitBreaker",
    "CircuitBreakerException",
    "ExecutionState",
    "BrenducateClient",
    "wrap_openai",
    "BrenducateCallbackHandler",
]
