"""
Brenducate SDK — Core Monitor, Tenant Context, & Trace Runner

Supports:
  - `with sentinel.tenant("acme_corp"):` for clean multi-tenant attribution
  - `with sentinel.trace(agent_id="bot", tenant_id="acme_corp"):`
  - `@sentinel.monitor(agent_id="bot", tenant_id="acme_corp")` (sync & async)
  - `sentinel.flush()` / `await sentinel.aflush()` for serverless runtimes
"""

import contextvars
import functools
import inspect
import os
import time
from dataclasses import dataclass
from typing import Any, Callable, Optional

from .circuit_breaker import CircuitBreaker, CircuitBreakerException, ExecutionState
from .client import BrenducateClient
from .pricing import calculate_token_cost


@dataclass
class TraceContext:
    """Active execution context propagated across async tasks and threads via ContextVar."""

    agent_id: str
    tenant_id: str
    model: str
    breaker: CircuitBreaker
    state: ExecutionState
    pulse: "Brenducate"


_current_trace_context: contextvars.ContextVar[Optional[TraceContext]] = (
    contextvars.ContextVar("_current_trace_context", default=None)
)

_current_tenant_id: contextvars.ContextVar[Optional[str]] = (
    contextvars.ContextVar("_current_tenant_id", default=None)
)


class _TenantScope:
    """Dual sync/async context manager setting active tenant_id."""

    def __init__(self, tenant_id: str) -> None:
        self.tenant_id = tenant_id
        self._token: Optional[contextvars.Token] = None

    def __enter__(self) -> str:
        self._token = _current_tenant_id.set(self.tenant_id)
        return self.tenant_id

    def __exit__(self, exc_type, exc_val, exc_tb) -> bool:
        if self._token is not None:
            _current_tenant_id.reset(self._token)
        return False

    async def __aenter__(self) -> str:
        return self.__enter__()

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> bool:
        return self.__exit__(exc_type, exc_val, exc_tb)


class _TraceContextManager:
    """Dual sync/async context manager wrapping an agent workflow run."""

    def __init__(
        self,
        pulse: "Brenducate",
        agent_id: str,
        model: str,
        tenant_id: str,
        breaker: CircuitBreaker,
    ) -> None:
        self.pulse = pulse
        self.agent_id = agent_id
        self.model = model
        self.tenant_id = tenant_id
        self.breaker = breaker
        self.state = ExecutionState(original_model=model, executed_model=model)
        self.start_ms = 0.0
        self._ctx_token: Optional[contextvars.Token] = None
        self._tenant_token: Optional[contextvars.Token] = None

    def __enter__(self) -> "_TraceContextManager":
        self.start_ms = time.monotonic()
        ctx = TraceContext(
            agent_id=self.agent_id,
            tenant_id=self.tenant_id,
            model=self.model,
            breaker=self.breaker,
            state=self.state,
            pulse=self.pulse,
        )
        self._ctx_token = _current_trace_context.set(ctx)
        self._tenant_token = _current_tenant_id.set(self.tenant_id)
        return self

    def record_step(self, *args, **kwargs) -> None:
        self.pulse.record_step(*args, **kwargs)

    def __exit__(self, exc_type, exc_val, exc_tb) -> bool:
        if self._ctx_token is not None:
            _current_trace_context.reset(self._ctx_token)
        if self._tenant_token is not None:
            _current_tenant_id.reset(self._tenant_token)

        latency_ms = int((time.monotonic() - self.start_ms) * 1000)
        status = "DEGRADED" if self.state.was_degraded else "COMPLETED"
        circuit_tripped = False
        guardrail_reason = None

        if exc_type and issubclass(exc_type, CircuitBreakerException):
            status = "BUDGET_BLOCKED" if "daily" in str(exc_val.reason).lower() or "blocked" in str(exc_val.reason).lower() else "CIRCUIT_KILLED"
            circuit_tripped = True
            guardrail_reason = exc_val.reason

        self.pulse._ship_trace(
            agent_id=self.agent_id,
            tenant_id=self.tenant_id,
            original_model=self.state.original_model or self.model,
            executed_model=self.state.executed_model or self.state.degraded_model or self.model,
            was_degraded=self.state.was_degraded,
            prompt="Context manager trace",
            state=self.state,
            status=status,
            latency_ms=latency_ms,
            guardrail_reason=guardrail_reason,
            circuit_tripped=circuit_tripped,
        )
        return False

    async def __aenter__(self) -> "_TraceContextManager":
        return self.__enter__()

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> bool:
        return self.__exit__(exc_type, exc_val, exc_tb)


class Brenducate:
    """
    Main SDK entry point for Brenducate Per-Tenant Margin Guardrails.

    Args:
        api_key:                Your `br_live_...` API key (or BRENDUCATE_API_KEY env var).
        base_url:               Backend URL (defaults to https://brenducate.com or BRENDUCATE_BASE_URL).
        environment:            "production", "staging", or "development".
        verbose:                Print concise trace summaries to stdout.
        enable_background_sync: Poll tenant budgets/policies in background (60s interval).
        register_atexit:        Flush queued traces cleanly on process shutdown.
    """

    def __init__(
        self,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        environment: str = "production",
        verbose: bool = False,
        enable_background_sync: bool = True,
        register_atexit: bool = True,
    ) -> None:
        self.api_key = api_key or os.getenv("BRENDUCATE_API_KEY", "br_live_demo_key_00001")
        self.base_url = (
            base_url
            or os.getenv("BRENDUCATE_BASE_URL", "https://brenducate.com")
        ).rstrip("/")
        self.environment = environment
        self.verbose = verbose
        self._client = BrenducateClient(
            api_key=self.api_key,
            base_url=self.base_url,
            enable_background_sync=enable_background_sync,
            register_atexit=register_atexit,
        )

    @classmethod
    def get_current_context(cls) -> Optional[TraceContext]:
        return _current_trace_context.get()

    @classmethod
    def get_current_tenant_id(cls) -> Optional[str]:
        tid = _current_tenant_id.get()
        if tid:
            return tid
        ctx = _current_trace_context.get()
        return ctx.tenant_id if ctx else None

    def tenant(self, tenant_id: str) -> _TenantScope:
        """Bind end-customer `tenant_id` for all nested LLM calls."""
        return _TenantScope(tenant_id=tenant_id)

    def trace(
        self,
        agent_id: str,
        model: str = "claude-sonnet-4",
        tenant_id: Optional[str] = None,
        circuit_breaker: Optional[CircuitBreaker] = None,
    ) -> _TraceContextManager:
        """Sync & async context manager wrapping a multi-step agent execution."""
        effective_tenant = tenant_id or _current_tenant_id.get() or "default"
        return _TraceContextManager(
            pulse=self,
            agent_id=agent_id,
            model=model,
            tenant_id=effective_tenant,
            breaker=circuit_breaker or CircuitBreaker(),
        )

    def flush(self, timeout: float = 2.0) -> int:
        """Synchronously flush all pending telemetry events (recommended in AWS Lambda / Modal)."""
        return self._client.flush(timeout=timeout)

    async def aflush(self, timeout: float = 2.0) -> int:
        """Asynchronously flush all pending telemetry events."""
        return await self._client.aflush(timeout=timeout)

    def close(self) -> None:
        self._client.flush(timeout=2.0)
        self._client.stop()

    def record_step(
        self,
        step_type: str = "tool",
        title: Optional[str] = None,
        content: Optional[str] = None,
        tool_call_str: Optional[str] = None,
        input_tokens: int = 0,
        output_tokens: int = 0,
        cached_input_tokens: int = 0,
        cache_creation_tokens: int = 0,
        reasoning_tokens: int = 0,
        cost_usd: Optional[float] = None,
    ) -> None:
        """Manually record a step inside an active `@monitor` or `with trace(...)` run."""
        ctx = _current_trace_context.get()
        if not ctx:
            return

        ctx.state.record_hop(
            breaker=ctx.breaker,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cached_input_tokens=cached_input_tokens,
            cache_creation_tokens=cache_creation_tokens,
            reasoning_tokens=reasoning_tokens,
            cost_usd=cost_usd,
            tool_call_str=tool_call_str,
            step_type=step_type,
            title=title,
            content=content,
            model_for_cost=ctx.state.degraded_model or ctx.model,
            tenant_id=ctx.tenant_id,
        )

    def monitor(
        self,
        agent_id: str,
        model: str = "claude-sonnet-4",
        tenant_id: Optional[str] = None,
        circuit_breaker: Optional[CircuitBreaker] = None,
    ) -> Callable:
        """Decorator wrapping sync or async agent workflows with margin & loop guardrails."""
        breaker = circuit_breaker or CircuitBreaker()

        def decorator(fn: Callable) -> Callable:
            if inspect.iscoroutinefunction(fn):
                @functools.wraps(fn)
                async def async_wrapper(*args, **kwargs):
                    return await self._run_async(
                        fn, args, kwargs, agent_id, model, tenant_id, breaker
                    )

                return async_wrapper

            @functools.wraps(fn)
            def sync_wrapper(*args, **kwargs):
                return self._run_sync(
                    fn, args, kwargs, agent_id, model, tenant_id, breaker
                )

            return sync_wrapper

        return decorator

    def _finalize_and_ship(
        self,
        agent_id: str,
        tenant_id: str,
        model: str,
        prompt: str,
        state: ExecutionState,
        start_ms: float,
        result: Any,
        status: str,
        exception: Optional[BaseException],
    ) -> None:
        latency_ms = int((time.monotonic() - start_ms) * 1000)
        if status == "COMPLETED" and result is not None:
            if state.was_degraded:
                status = "DEGRADED"
            state.add_step(
                step_type="final",
                title="Response Completed",
                content=f"Result: {str(result)[:300]}",
            )

        guardrail_reason = None
        circuit_tripped = False
        if isinstance(exception, CircuitBreakerException):
            circuit_tripped = True
            guardrail_reason = exception.reason
        elif status == "ERROR" and exception is not None:
            guardrail_reason = str(exception)

        self._ship_trace(
            agent_id=agent_id,
            tenant_id=tenant_id,
            original_model=state.original_model or model,
            executed_model=state.executed_model or state.degraded_model or model,
            was_degraded=state.was_degraded,
            prompt=prompt,
            state=state,
            status=status,
            latency_ms=latency_ms,
            guardrail_reason=guardrail_reason,
            circuit_tripped=circuit_tripped,
        )

    def _run_sync(
        self,
        fn: Callable,
        args: tuple,
        kwargs: dict,
        agent_id: str,
        model: str,
        tenant_id: Optional[str],
        breaker: CircuitBreaker,
    ) -> Any:
        effective_tenant = (
            kwargs.get("tenant_id") or tenant_id or _current_tenant_id.get() or "default"
        )
        state = ExecutionState(original_model=model, executed_model=model)
        ctx = TraceContext(
            agent_id=agent_id,
            tenant_id=effective_tenant,
            model=model,
            breaker=breaker,
            state=state,
            pulse=self,
        )
        ctx_token = _current_trace_context.set(ctx)
        tenant_token = _current_tenant_id.set(effective_tenant)
        start_ms = time.monotonic()
        prompt = self._extract_prompt(args, kwargs)
        result = None
        status = "COMPLETED"
        exception: Optional[BaseException] = None

        state.add_step(
            step_type="thought",
            title="Workflow Initiated",
            content=f"Agent '{agent_id}' (tenant='{effective_tenant}') started.",
        )

        try:
            result = fn(*args, **kwargs)
            self._auto_detect_usage(result, state, state.degraded_model or model)
        except CircuitBreakerException as e:
            status = "BUDGET_BLOCKED" if "daily" in e.reason.lower() or "blocked" in e.reason.lower() else "CIRCUIT_KILLED"
            exception = e
            state.add_step(
                step_type="guardrail",
                title="Guardrail Triggered",
                content=f"Execution halted: {e.reason}",
            )
        except Exception as e:
            status = "ERROR"
            exception = e
            state.add_step(
                step_type="guardrail",
                title="Execution Error",
                content=str(e),
            )
        finally:
            _current_trace_context.reset(ctx_token)
            _current_tenant_id.reset(tenant_token)

        self._finalize_and_ship(
            agent_id=agent_id,
            tenant_id=effective_tenant,
            model=model,
            prompt=prompt,
            state=state,
            start_ms=start_ms,
            result=result,
            status=status,
            exception=exception,
        )
        if exception is not None:
            raise exception
        return result

    async def _run_async(
        self,
        fn: Callable,
        args: tuple,
        kwargs: dict,
        agent_id: str,
        model: str,
        tenant_id: Optional[str],
        breaker: CircuitBreaker,
    ) -> Any:
        effective_tenant = (
            kwargs.get("tenant_id") or tenant_id or _current_tenant_id.get() or "default"
        )
        state = ExecutionState(original_model=model, executed_model=model)
        ctx = TraceContext(
            agent_id=agent_id,
            tenant_id=effective_tenant,
            model=model,
            breaker=breaker,
            state=state,
            pulse=self,
        )
        ctx_token = _current_trace_context.set(ctx)
        tenant_token = _current_tenant_id.set(effective_tenant)
        start_ms = time.monotonic()
        prompt = self._extract_prompt(args, kwargs)
        result = None
        status = "COMPLETED"
        exception: Optional[BaseException] = None

        state.add_step(
            step_type="thought",
            title="Workflow Initiated",
            content=f"Agent '{agent_id}' (tenant='{effective_tenant}') started.",
        )

        try:
            result = await fn(*args, **kwargs)
            self._auto_detect_usage(result, state, state.degraded_model or model)
        except CircuitBreakerException as e:
            status = "BUDGET_BLOCKED" if "daily" in e.reason.lower() or "blocked" in e.reason.lower() else "CIRCUIT_KILLED"
            exception = e
            state.add_step(
                step_type="guardrail",
                title="Guardrail Triggered",
                content=f"Execution halted: {e.reason}",
            )
        except Exception as e:
            status = "ERROR"
            exception = e
            state.add_step(
                step_type="guardrail",
                title="Execution Error",
                content=str(e),
            )
        finally:
            _current_trace_context.reset(ctx_token)
            _current_tenant_id.reset(tenant_token)

        self._finalize_and_ship(
            agent_id=agent_id,
            tenant_id=effective_tenant,
            model=model,
            prompt=prompt,
            state=state,
            start_ms=start_ms,
            result=result,
            status=status,
            exception=exception,
        )
        if exception is not None:
            raise exception
        return result

    def _auto_detect_usage(self, result: Any, state: ExecutionState, model: str) -> None:
        if state.total_tokens > 0:
            return
        usage = getattr(result, "usage", None) if not isinstance(result, dict) else result.get("usage")
        if not usage:
            return
        in_tok = (
            usage.get("prompt_tokens", usage.get("input_tokens", 0))
            if isinstance(usage, dict)
            else getattr(usage, "prompt_tokens", getattr(usage, "input_tokens", 0))
        ) or 0
        out_tok = (
            usage.get("completion_tokens", usage.get("output_tokens", 0))
            if isinstance(usage, dict)
            else getattr(usage, "completion_tokens", getattr(usage, "output_tokens", 0))
        ) or 0
        if in_tok or out_tok:
            state.input_tokens += in_tok
            state.output_tokens += out_tok
            state.cost_usd += calculate_token_cost(model, in_tok, out_tok)
            if state.hops == 0:
                state.hops = 1

    def _extract_prompt(self, args: tuple, kwargs: dict) -> str:
        for key in ("prompt", "query", "goal", "task", "message", "input"):
            if key in kwargs and isinstance(kwargs[key], str):
                return kwargs[key][:500]
        for arg in args:
            if isinstance(arg, str):
                return arg[:500]
        return "(no prompt captured)"

    def _ship_trace(
        self,
        agent_id: str,
        tenant_id: str,
        original_model: str,
        executed_model: str,
        was_degraded: bool,
        prompt: str,
        state: ExecutionState,
        status: str,
        latency_ms: int,
        guardrail_reason: Optional[str],
        circuit_tripped: bool,
    ) -> None:
        self._client.policy_store.record_local_spend(tenant_id, state.cost_usd)
        payload = {
            "tenant_id": tenant_id,
            "agent_id": agent_id,
            "original_model": original_model,
            "executed_model": executed_model,
            "was_degraded": was_degraded,
            "prompt": prompt[:500],
            "status": status,
            "input_tokens": state.input_tokens,
            "cached_input_tokens": state.cached_input_tokens,
            "cache_creation_tokens": state.cache_creation_tokens,
            "output_tokens": state.output_tokens,
            "reasoning_tokens": state.reasoning_tokens,
            "cost_usd": round(state.cost_usd, 6),
            "estimated_savings_usd": round(state.saved_cost_usd, 6),
            "latency_ms": latency_ms,
            "hops": state.hops,
            "circuit_tripped": circuit_tripped,
            "guardrail_reason": guardrail_reason,
            "steps": state.steps,
            "environment": self.environment,
        }

        if self.verbose:
            tag = "DEGRADED" if was_degraded else status
            print(
                f"[Brenducate] tenant='{tenant_id}' | agent='{agent_id}' | "
                f"model={executed_model} | status={tag} | cost=${state.cost_usd:.4f} "
                f"| saved=${state.saved_cost_usd:.4f}"
            )

        self._client.send_trace(payload)


# Backward-compatible alias if existing code imports AgentPulse directly
AgentPulse = Brenducate
