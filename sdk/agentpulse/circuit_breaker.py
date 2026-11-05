"""
AgentPulse SDK — Circuit Breaker Engine

Tracks per-execution state, steps, token consumption, and raises
CircuitBreakerException when any guardrail threshold is crossed.
Runs entirely in-process with zero network overhead (<1ms kill decision).
"""

from dataclasses import dataclass, field
from typing import Optional, List, Dict, Any


# Standard model pricing per 1,000,000 tokens (USD)
MODEL_PRICING: Dict[str, Dict[str, float]] = {
    "claude-3-7-sonnet":   {"input": 3.00,  "output": 15.00},
    "claude-3-5-sonnet":   {"input": 3.00,  "output": 15.00},
    "claude-3-5-haiku":    {"input": 0.80,  "output": 4.00},
    "gpt-4o":              {"input": 2.50,  "output": 10.00},
    "gpt-4o-mini":         {"input": 0.15,  "output": 0.60},
    "o1":                  {"input": 15.00, "output": 60.00},
    "o3-mini":             {"input": 1.10,  "output": 4.40},
    "gemini-2.5-pro":      {"input": 1.25,  "output": 5.00},
    "gemini-2.5-flash":    {"input": 0.10,  "output": 0.40},
    "llama-3.3-70b":       {"input": 0.60,  "output": 1.80},
    "deepseek-r1":         {"input": 0.55,  "output": 2.19},
    "default":             {"input": 2.00,  "output": 8.00},
}


def calculate_token_cost(model_name: str, input_tokens: int, output_tokens: int) -> float:
    """Calculate USD cost based on standard model token pricing."""
    name_clean = model_name.lower().strip()
    pricing = MODEL_PRICING.get("default")
    for key, p in MODEL_PRICING.items():
        if key in name_clean:
            pricing = p
            break
    
    in_cost = (input_tokens / 1_000_000.0) * pricing["input"]
    out_cost = (output_tokens / 1_000_000.0) * pricing["output"]
    return round(in_cost + out_cost, 6)


class CircuitBreakerException(Exception):
    """Raised when a guardrail threshold is crossed and execution is aborted."""
    def __init__(self, reason: str, hops: int, cost_usd: float, tokens: int):
        self.reason = reason
        self.hops = hops
        self.cost_usd = cost_usd
        self.tokens = tokens
        super().__init__(f"[AgentPulse] Circuit breaker tripped: {reason}")


@dataclass
class CircuitBreaker:
    """
    Configurable guardrail policy for a monitored agent.

    Args:
        max_hops:              Kill if the agent makes more than this many
                               sequential tool calls / reasoning steps.
        max_cost_usd:          Kill if the estimated token cost for a single
                               run exceeds this dollar amount.
        kill_on_infinite_loop: Kill if the exact same tool call string is
                               repeated consecutively more than max_duplicate_calls times.
        max_duplicate_calls:   Duplicate call threshold (default: 2).
    """
    max_hops:              int   = 8
    max_cost_usd:          float = 1.00
    kill_on_infinite_loop: bool  = True
    max_duplicate_calls:   int   = 2


@dataclass
class ExecutionState:
    """Mutable run-time state tracked per agent execution."""
    hops:               int = 0
    input_tokens:       int = 0
    output_tokens:      int = 0
    cost_usd:           float = 0.0
    steps:              List[Dict[str, Any]] = field(default_factory=list)
    _tool_call_history: List[str] = field(default_factory=list)

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    def add_step(
        self,
        step_type: str,
        title: str,
        content: str,
        tool_call: Optional[str] = None
    ) -> None:
        """Add a step to the execution graph."""
        self.steps.append({
            "step_index": len(self.steps) + 1,
            "step_type": step_type,
            "title": title,
            "content": content,
            "tool_call": tool_call,
        })

    def record_hop(
        self,
        breaker:        CircuitBreaker,
        input_tokens:   int   = 0,
        output_tokens:  int   = 0,
        cost_usd:       Optional[float] = None,
        tool_call_str:  Optional[str] = None,
        step_type:      str = "tool",
        title:          Optional[str] = None,
        content:        Optional[str] = None,
        model_for_cost: Optional[str] = None,
    ) -> None:
        """
        Record a single reasoning step or tool call.
        Raises CircuitBreakerException immediately if any guardrail is violated.
        """
        self.hops += 1
        self.input_tokens += input_tokens
        self.output_tokens += output_tokens

        # Auto-calculate cost if not provided explicitly
        if cost_usd is not None:
            hop_cost = cost_usd
        elif model_for_cost:
            hop_cost = calculate_token_cost(model_for_cost, input_tokens, output_tokens)
        else:
            hop_cost = 0.0

        self.cost_usd += hop_cost

        # Add to step history
        step_title = title or (f"Tool Call: {tool_call_str[:40]}..." if tool_call_str else f"Step {self.hops}")
        step_content = content or (tool_call_str or f"Completed hop {self.hops}")
        self.add_step(
            step_type=step_type,
            title=step_title,
            content=step_content,
            tool_call=tool_call_str,
        )

        # ── Guardrail 1: Maximum hop count ──────────────────────────────────
        if self.hops > breaker.max_hops:
            raise CircuitBreakerException(
                reason=f"Maximum hop limit exceeded ({self.hops} > {breaker.max_hops})",
                hops=self.hops,
                cost_usd=self.cost_usd,
                tokens=self.total_tokens,
            )

        # ── Guardrail 2: Cost ceiling ────────────────────────────────────────
        if self.cost_usd > breaker.max_cost_usd:
            raise CircuitBreakerException(
                reason=f"Single-run cost ceiling exceeded (${self.cost_usd:.4f} > ${breaker.max_cost_usd:.2f})",
                hops=self.hops,
                cost_usd=self.cost_usd,
                tokens=self.total_tokens,
            )

        # ── Guardrail 3: Infinite loop / repetitive tool call ────────────────
        if breaker.kill_on_infinite_loop and tool_call_str:
            self._tool_call_history.append(tool_call_str)
            tail = self._tool_call_history[-breaker.max_duplicate_calls - 1:]
            if (
                len(tail) > breaker.max_duplicate_calls
                and len(set(tail)) == 1
            ):
                raise CircuitBreakerException(
                    reason=(
                        f"Repetitive tool call detected: same call repeated "
                        f"{breaker.max_duplicate_calls + 1}x consecutively: '{tool_call_str[:80]}'"
                    ),
                    hops=self.hops,
                    cost_usd=self.cost_usd,
                    tokens=self.total_tokens,
                )
