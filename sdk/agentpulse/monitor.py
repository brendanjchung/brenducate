"""
AgentPulse SDK — @monitor Decorator & Trace Context Manager

Production-grade observability and sub-millisecond circuit breakers for LLM agents.
Supports contextvars for seamless nested tracking across tools and functions.
"""

import time
import functools
import inspect
import contextvars
from dataclasses import dataclass
from typing import Optional, Callable, Any, Dict

from .circuit_breaker import (
    CircuitBreaker, CircuitBreakerException, ExecutionState,
    calculate_token_cost
)
from .client import AgentPulseClient


@dataclass
class TraceContext:
    """Active execution context stored in thread-safe / async-safe ContextVar."""
    agent_id: str
    model:    str
    breaker:  CircuitBreaker
    state:    ExecutionState
    pulse:    "AgentPulse"


_current_trace_context: contextvars.ContextVar[Optional[TraceContext]] = (
    contextvars.ContextVar("_current_trace_context", default=None)
)


class AgentPulse:
    """
    Main SDK entry point for AgentPulse Ops.

    Args:
        api_key:     AgentPulse API key (e.g. "ap_live_...").
        base_url:    Base URL of the AgentPulse backend (default: http://localhost:8000).
        environment: Tag traces as "production", "staging", or "development".
        verbose:     Print trace summaries to stdout.
    """

    def __init__(
        self,
        api_key:     str,
        base_url:    str = "http://localhost:8000",
        environment: str = "production",
        verbose:     bool = True,
    ):
        self.api_key     = api_key
        self.base_url    = base_url
        self.environment = environment
        self.verbose     = verbose
        self._client     = AgentPulseClient(api_key=api_key, base_url=base_url)

    # ── Context Record Helper ──────────────────────────────────────────────────

    @classmethod
    def get_current_context(cls) -> Optional[TraceContext]:
        """Retrieve the active trace context from ContextVars."""
        return _current_trace_context.get()

    def record_step(
        self,
        step_type:     str = "tool",
        title:         Optional[str] = None,
        content:       Optional[str] = None,
        tool_call_str: Optional[str] = None,
        input_tokens:  int = 0,
        output_tokens: int = 0,
        cost_usd:      Optional[float] = None,
    ) -> None:
        """
        Record a reasoning step or tool call for the current active agent run.
        Enforces circuit breaker rules and raises CircuitBreakerException if limits trip.
        """
        ctx = _current_trace_context.get()
        if not ctx:
            return

        ctx.state.record_hop(
            breaker=ctx.breaker,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cost_usd=cost_usd,
            tool_call_str=tool_call_str,
            step_type=step_type,
            title=title,
            content=content,
            model_for_cost=ctx.model,
        )

    # ── Decorator ──────────────────────────────────────────────────────────────

    def monitor(
        self,
        agent_id:        str,
        model:           str = "claude-3-7-sonnet",
        circuit_breaker: Optional[CircuitBreaker] = None,
    ) -> Callable:
        """
        Decorator for agent workflows. Tracks runtime, tokens, cost, and intercepts runaway loops.
        """
        if circuit_breaker is None:
            circuit_breaker = CircuitBreaker()

        def decorator(fn: Callable) -> Callable:
            is_async = inspect.iscoroutinefunction(fn)

            if is_async:
                @functools.wraps(fn)
                async def async_wrapper(*args, **kwargs):
                    return await self._run_async(fn, args, kwargs, agent_id, model, circuit_breaker)
                return async_wrapper
            else:
                @functools.wraps(fn)
                def sync_wrapper(*args, **kwargs):
                    return self._run_sync(fn, args, kwargs, agent_id, model, circuit_breaker)
                return sync_wrapper

        return decorator

    # ── Sync Runner ────────────────────────────────────────────────────────────

    def _run_sync(
        self, fn, args, kwargs,
        agent_id: str, model: str, breaker: CircuitBreaker
    ) -> Any:
        state    = ExecutionState()
        ctx      = TraceContext(agent_id=agent_id, model=model, breaker=breaker, state=state, pulse=self)
        token    = _current_trace_context.set(ctx)
        start_ms = time.monotonic()
        prompt   = self._extract_prompt(args, kwargs)
        result   = None
        status   = "COMPLETED"
        exception = None

        # Add initial planning step
        state.add_step(
            step_type="thought",
            title="Workflow Initiated",
            content=f"Agent '{agent_id}' started with prompt: {prompt[:120]}...",
        )

        try:
            result = fn(*args, **kwargs)
            self._auto_detect_usage(result, state, model)
        except CircuitBreakerException as e:
            status = "CIRCUIT_KILLED"
            exception = e
            # Sync state with exception data
            if state.hops < e.hops:
                state.hops = e.hops
            if state.cost_usd < e.cost_usd:
                state.cost_usd = e.cost_usd
            if state.total_tokens < e.tokens:
                state.input_tokens = e.tokens
            state.add_step(
                step_type="guardrail",
                title="Circuit Breaker Tripped",
                content=f"Execution forcibly aborted: {e.reason}",
            )
        except Exception as e:
            status = "ERROR"
            exception = e
            state.add_step(
                step_type="guardrail",
                title="Execution Error",
                content=f"Unhandled exception: {str(e)}",
            )
        finally:
            _current_trace_context.reset(token)

        latency_ms = int((time.monotonic() - start_ms) * 1000)

        # Add final answer step if completed cleanly
        if status == "COMPLETED" and result is not None:
            res_str = str(result)[:300]
            state.add_step(
                step_type="final",
                title="Response Synthesized",
                content=f"Result: {res_str}",
            )

        self._ship_trace(
            agent_id=agent_id,
            model=model,
            prompt=prompt,
            state=state,
            status=status,
            latency_ms=latency_ms,
            guardrail_reason=exception.reason if isinstance(exception, CircuitBreakerException) else (str(exception) if status == "ERROR" else None),
            circuit_tripped=isinstance(exception, CircuitBreakerException),
        )

        if exception:
            raise exception
        return result

    # ── Async Runner ───────────────────────────────────────────────────────────

    async def _run_async(
        self, fn, args, kwargs,
        agent_id: str, model: str, breaker: CircuitBreaker
    ) -> Any:
        state    = ExecutionState()
        ctx      = TraceContext(agent_id=agent_id, model=model, breaker=breaker, state=state, pulse=self)
        token    = _current_trace_context.set(ctx)
        start_ms = time.monotonic()
        prompt   = self._extract_prompt(args, kwargs)
        result   = None
        status   = "COMPLETED"
        exception = None

        state.add_step(
            step_type="thought",
            title="Workflow Initiated",
            content=f"Agent '{agent_id}' started with prompt: {prompt[:120]}...",
        )

        try:
            result = await fn(*args, **kwargs)
            self._auto_detect_usage(result, state, model)
        except CircuitBreakerException as e:
            status = "CIRCUIT_KILLED"
            exception = e
            if state.hops < e.hops:
                state.hops = e.hops
            if state.cost_usd < e.cost_usd:
                state.cost_usd = e.cost_usd
            if state.total_tokens < e.tokens:
                state.input_tokens = e.tokens
            state.add_step(
                step_type="guardrail",
                title="Circuit Breaker Tripped",
                content=f"Execution forcibly aborted: {e.reason}",
            )
        except Exception as e:
            status = "ERROR"
            exception = e
            state.add_step(
                step_type="guardrail",
                title="Execution Error",
                content=f"Unhandled exception: {str(e)}",
            )
        finally:
            _current_trace_context.reset(token)

        latency_ms = int((time.monotonic() - start_ms) * 1000)

        if status == "COMPLETED" and result is not None:
            res_str = str(result)[:300]
            state.add_step(
                step_type="final",
                title="Response Synthesized",
                content=f"Result: {res_str}",
            )

        self._ship_trace(
            agent_id=agent_id,
            model=model,
            prompt=prompt,
            state=state,
            status=status,
            latency_ms=latency_ms,
            guardrail_reason=exception.reason if isinstance(exception, CircuitBreakerException) else (str(exception) if status == "ERROR" else None),
            circuit_tripped=isinstance(exception, CircuitBreakerException),
        )

        if exception:
            raise exception
        return result

    # ── Context Manager (Alternative to Decorator) ─────────────────────────────

    class _TraceContextManager:
        def __init__(self, pulse: "AgentPulse", agent_id: str, model: str, breaker: CircuitBreaker):
            self.pulse = pulse
            self.agent_id = agent_id
            self.model = model
            self.breaker = breaker
            self.state = ExecutionState()
            self.start_ms = 0
            self.token = None

        def __enter__(self):
            self.start_ms = time.monotonic()
            ctx = TraceContext(
                agent_id=self.agent_id,
                model=self.model,
                breaker=self.breaker,
                state=self.state,
                pulse=self.pulse
            )
            self.token = _current_trace_context.set(ctx)
            return self

        def record_step(self, *args, **kwargs):
            self.pulse.record_step(*args, **kwargs)

        def __exit__(self, exc_type, exc_val, exc_tb):
            _current_trace_context.reset(self.token)
            latency_ms = int((time.monotonic() - self.start_ms) * 1000)
            status = "COMPLETED"
            circuit_tripped = False
            guardrail_reason = None

            if exc_type and issubclass(exc_type, CircuitBreakerException):
                status = "CIRCUIT_KILLED"
                circuit_tripped = True
                guardrail_reason = exc_val.reason

            self.pulse._ship_trace(
                agent_id=self.agent_id,
                model=self.model,
                prompt="Context manager trace",
                state=self.state,
                status=status,
                latency_ms=latency_ms,
                guardrail_reason=guardrail_reason,
                circuit_tripped=circuit_tripped,
            )
            return False

    def trace(
        self,
        agent_id: str,
        model: str = "claude-3-7-sonnet",
        circuit_breaker: Optional[CircuitBreaker] = None,
    ):
        """Context manager support: `with pulse.trace('bot') as t: ...`"""
        return self._TraceContextManager(
            pulse=self,
            agent_id=agent_id,
            model=model,
            breaker=circuit_breaker or CircuitBreaker()
        )

    # ── Helpers ────────────────────────────────────────────────────────────────

    def _auto_detect_usage(self, result: Any, state: ExecutionState, model: str):
        """Attempt to extract tokens from popular client responses (OpenAI, Anthropic, LangChain)."""
        if state.total_tokens > 0:
            return  # Already recorded manually

        usage = None
        if hasattr(result, "usage") and result.usage:
            usage = result.usage
        elif isinstance(result, dict) and "usage" in result:
            usage = result["usage"]

        if usage:
            in_tok = getattr(usage, "prompt_tokens", getattr(usage, "input_tokens", 0)) or 0
            out_tok = getattr(usage, "completion_tokens", getattr(usage, "output_tokens", 0)) or 0
            if isinstance(usage, dict):
                in_tok = usage.get("prompt_tokens", usage.get("input_tokens", in_tok))
                out_tok = usage.get("completion_tokens", usage.get("output_tokens", out_tok))
            
            if in_tok or out_tok:
                state.input_tokens += in_tok
                state.output_tokens += out_tok
                state.cost_usd += calculate_token_cost(model, in_tok, out_tok)
                if state.hops == 0:
                    state.hops = 1

    def _extract_prompt(self, args, kwargs) -> str:
        for key in ("prompt", "query", "goal", "task", "message", "input"):
            if key in kwargs and isinstance(kwargs[key], str):
                return kwargs[key][:500]
        for arg in args:
            if isinstance(arg, str):
                return arg[:500]
        return "(no prompt captured)"

    def _ship_trace(
        self,
        agent_id:         str,
        model:            str,
        prompt:           str,
        state:            ExecutionState,
        status:           str,
        latency_ms:       int,
        guardrail_reason: Optional[str],
        circuit_tripped:  bool,
    ) -> None:
        payload = {
            "agent_id":         agent_id,
            "model":            model,
            "prompt":           prompt,
            "status":           status,
            "input_tokens":     state.input_tokens,
            "output_tokens":    state.output_tokens,
            "cost_usd":         round(state.cost_usd, 6),
            "latency_ms":       latency_ms,
            "hops":             state.hops,
            "circuit_tripped":  circuit_tripped,
            "guardrail_reason": guardrail_reason,
            "steps":            state.steps,
            "environment":      self.environment,
        }

        if self.verbose:
            status_icon = "✅" if status == "COMPLETED" else "🚨" if circuit_tripped else "⚠️"
            print(
                f"\n{status_icon} [AgentPulse] agent='{agent_id}' | "
                f"status={status} | "
                f"hops={state.hops} | "
                f"tokens={state.total_tokens:,} | "
                f"cost=${state.cost_usd:.4f} | "
                f"latency={latency_ms}ms"
            )
            if guardrail_reason:
                print(f"   ⤷ Guardrail: {guardrail_reason}")

        self._client.send_trace(payload)
