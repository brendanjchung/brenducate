"""
Brenducate SDK — In-Process Circuit Breaker & Smart Degradation State

Runs strictly in-memory (<1ms) with zero network latency on the critical path.
Raises CircuitBreakerException (or TenantBudgetExceeded) immediately if a
hard limit or infinite tool loop threshold is breached.
"""

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from .pricing import (
    TokenUsage,
    calculate_cost,
    calculate_degradation_savings,
    calculate_token_cost,
    resolve_step_down_model,
)


class CircuitBreakerException(Exception):
    """Raised when a guardrail threshold is crossed and execution is halted."""

    def __init__(
        self,
        reason: str,
        hops: int = 0,
        cost_usd: float = 0.0,
        tokens: int = 0,
        tenant_id: str = "default",
    ):
        self.reason = reason
        self.hops = hops
        self.cost_usd = cost_usd
        self.tokens = tokens
        self.tenant_id = tenant_id
        super().__init__(f"[Brenducate] Guardrail tripped (tenant={tenant_id}): {reason}")


class TenantBudgetExceeded(CircuitBreakerException):
    """Raised when an end-customer tenant breaches its daily or hard spend cap."""


@dataclass
class CircuitBreaker:
    """
    In-process guardrail policy for per-run and per-tenant budget protection.

    Args:
        max_hops:               Abort run if steps exceed this count.
        max_cost_usd:           Hard cap per run — abort if exceeded.
        degrade_after_cost_usd: Soft cap per run — step down to cheaper same-family
                                model once exceeded (sticky for remainder of run).
        kill_on_infinite_loop:  Abort if the exact same tool call repeats consecutively.
        max_duplicate_calls:    Allowed identical repeats before halting (default: 2).
        fallback_map:           Optional custom model -> cheaper_model overrides.
    """

    max_hops: int = 12
    max_cost_usd: float = 1.50
    degrade_after_cost_usd: Optional[float] = 0.25
    kill_on_infinite_loop: bool = True
    max_duplicate_calls: int = 2
    fallback_map: Optional[Dict[str, str]] = None


@dataclass
class ExecutionState:
    """Mutable runtime state tracked across steps of a single monitored agent run."""

    hops: int = 0
    input_tokens: int = 0
    cached_input_tokens: int = 0
    cache_creation_tokens: int = 0
    output_tokens: int = 0
    reasoning_tokens: int = 0
    cost_usd: float = 0.0
    saved_cost_usd: float = 0.0
    original_model: Optional[str] = None
    executed_model: Optional[str] = None
    degraded_model: Optional[str] = None  # Sticky same-family fallback for this run
    was_degraded: bool = False
    steps: List[Dict[str, Any]] = field(default_factory=list)
    _tool_call_history: List[str] = field(default_factory=list)

    @property
    def total_tokens(self) -> int:
        return (
            self.input_tokens
            + self.cached_input_tokens
            + self.cache_creation_tokens
            + self.output_tokens
        )

    def add_step(
        self,
        step_type: str,
        title: str,
        content: str,
        tool_call: Optional[str] = None,
    ) -> None:
        self.steps.append(
            {
                "step_index": len(self.steps) + 1,
                "step_type": step_type,
                "title": title,
                "content": content,
                "tool_call": tool_call,
            }
        )

    def maybe_activate_degradation(
        self,
        requested_model: str,
        breaker: CircuitBreaker,
    ) -> Optional[str]:
        """
        Return the sticky same-family fallback model if this run has already
        crossed `degrade_after_cost_usd`, activating it on the first crossing.
        """
        if self.degraded_model:
            return self.degraded_model
        if breaker.degrade_after_cost_usd is None:
            return None
        if self.cost_usd >= breaker.degrade_after_cost_usd:
            fallback = resolve_step_down_model(requested_model, breaker.fallback_map)
            if fallback and fallback.lower() != requested_model.lower():
                self.degraded_model = fallback
                self.was_degraded = True
                self.add_step(
                    step_type="degrade",
                    title="Smart Degradation Activated",
                    content=(
                        f"Run cost ${self.cost_usd:.4f} reached soft cap "
                        f"${breaker.degrade_after_cost_usd:.2f}. Stepping down "
                        f"{requested_model} -> {fallback} for remaining turns."
                    ),
                )
                return fallback
        return None

    def record_hop(
        self,
        breaker: CircuitBreaker,
        input_tokens: int = 0,
        output_tokens: int = 0,
        cached_input_tokens: int = 0,
        cache_creation_tokens: int = 0,
        reasoning_tokens: int = 0,
        cost_usd: Optional[float] = None,
        saved_cost_usd: float = 0.0,
        tool_call_str: Optional[str] = None,
        step_type: str = "tool",
        title: Optional[str] = None,
        content: Optional[str] = None,
        model_for_cost: Optional[str] = None,
        tenant_id: str = "default",
    ) -> None:
        """
        Record a completed reasoning step or LLM call and enforce local guardrails.
        Raises CircuitBreakerException immediately if any hard threshold is crossed.
        """
        self.hops += 1
        self.input_tokens += input_tokens
        self.cached_input_tokens += cached_input_tokens
        self.cache_creation_tokens += cache_creation_tokens
        self.output_tokens += output_tokens
        self.reasoning_tokens += reasoning_tokens

        usage = TokenUsage(
            input_tokens=input_tokens,
            cached_input_tokens=cached_input_tokens,
            cache_creation_tokens=cache_creation_tokens,
            output_tokens=output_tokens,
            reasoning_tokens=reasoning_tokens,
        )

        if cost_usd is not None:
            hop_cost = cost_usd
        elif model_for_cost:
            hop_cost = calculate_cost(model_for_cost, usage)
        else:
            hop_cost = 0.0

        self.cost_usd += hop_cost
        self.saved_cost_usd += saved_cost_usd

        step_title = title or (
            f"Tool Call: {tool_call_str[:40]}..." if tool_call_str else f"Step {self.hops}"
        )
        step_content = content or (tool_call_str or f"Completed hop {self.hops}")
        self.add_step(
            step_type=step_type,
            title=step_title,
            content=step_content,
            tool_call=tool_call_str,
        )

        # Guardrail 1: Maximum hop limit
        if self.hops > breaker.max_hops:
            raise CircuitBreakerException(
                reason=f"Maximum hop limit exceeded ({self.hops} > {breaker.max_hops})",
                hops=self.hops,
                cost_usd=self.cost_usd,
                tokens=self.total_tokens,
                tenant_id=tenant_id,
            )

        # Guardrail 2: Single-run hard cost ceiling
        if self.cost_usd > breaker.max_cost_usd:
            raise CircuitBreakerException(
                reason=f"Single-run hard cap exceeded (${self.cost_usd:.4f} > ${breaker.max_cost_usd:.2f})",
                hops=self.hops,
                cost_usd=self.cost_usd,
                tokens=self.total_tokens,
                tenant_id=tenant_id,
            )

        # Guardrail 3: Repetitive tool loop detection
        if breaker.kill_on_infinite_loop and tool_call_str:
            self._tool_call_history.append(tool_call_str)
            tail = self._tool_call_history[-breaker.max_duplicate_calls - 1:]
            if len(tail) > breaker.max_duplicate_calls and len(set(tail)) == 1:
                raise CircuitBreakerException(
                    reason=(
                        f"Repetitive tool loop detected ({breaker.max_duplicate_calls + 1}x "
                        f"identical calls): '{tool_call_str[:80]}'"
                    ),
                    hops=self.hops,
                    cost_usd=self.cost_usd,
                    tokens=self.total_tokens,
                    tenant_id=tenant_id,
                )
