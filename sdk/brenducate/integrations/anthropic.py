"""
Brenducate SDK — Anthropic Sync & Async Client Auto-Instrumentation

Supports both `Anthropic` and `AsyncAnthropic` across:
  - `client.messages.create(...)` (non-streaming and `stream=True`)
  - `client.messages.stream(...)` context manager (`with` / `async with`)

Accurately tracks Anthropic additive prompt caching buckets
(`input_tokens`, `cache_read_input_tokens`, `cache_creation_input_tokens`)
and executes same-family step-downs (e.g. Sonnet -> Haiku) preserving cache structure.
"""

import functools
import inspect
import os
import time
from typing import Any, Dict, Optional, Tuple

from ..circuit_breaker import (
    CircuitBreaker,
    CircuitBreakerException,
    ExecutionState,
    TenantBudgetExceeded,
)
from ..monitor import Brenducate
from ..pricing import calculate_cost, calculate_degradation_savings, extract_anthropic_usage


def _extract_tenant_header(kwargs: Dict[str, Any], default_tenant: Optional[str]) -> str:
    extra_headers = kwargs.get("extra_headers")
    if isinstance(extra_headers, dict):
        for k in list(extra_headers.keys()):
            if str(k).lower() == "x-brenducate-tenant":
                val = str(extra_headers[k]).strip()
                copied = dict(extra_headers)
                copied.pop(k, None)
                kwargs["extra_headers"] = copied
                if val:
                    return val
    return Brenducate.get_current_tenant_id() or default_tenant or "default"


def _extract_anthropic_prompt(kwargs: Dict[str, Any]) -> str:
    messages = kwargs.get("messages")
    if isinstance(messages, (list, tuple)) and messages:
        last_msg = messages[-1]
        content = (
            last_msg.get("content", "")
            if isinstance(last_msg, dict)
            else getattr(last_msg, "content", "")
        )
        if isinstance(content, list):
            parts = [
                p.get("text", "") if isinstance(p, dict) else getattr(p, "text", "")
                for p in content
            ]
            return " ".join(x for x in parts if x)[:500]
        return str(content or "")[:500]
    return ""


def _prepare_anthropic_call(
    pulse: Brenducate,
    breaker: CircuitBreaker,
    default_tenant: Optional[str],
    kwargs: Dict[str, Any],
) -> Tuple[Dict[str, Any], str, str, str, bool, str, Any]:
    call_kwargs = dict(kwargs)
    tenant_id = _extract_tenant_header(call_kwargs, default_tenant)
    requested_model = call_kwargs.get("model", "claude-sonnet-4-20250514")
    prompt_str = _extract_anthropic_prompt(call_kwargs)
    ctx = Brenducate.get_current_context()
    effective_breaker = ctx.breaker if ctx else breaker

    action, policy_model, reason = pulse._client.policy_store.check_tenant(
        tenant_id=tenant_id,
        requested_model=requested_model,
        custom_fallback_map=effective_breaker.fallback_map,
    )
    if action == "block":
        raise TenantBudgetExceeded(
            reason=reason or f"Tenant '{tenant_id}' exceeded daily cap.",
            hops=ctx.state.hops if ctx else 0,
            cost_usd=ctx.state.cost_usd if ctx else 0.0,
            tokens=ctx.state.total_tokens if ctx else 0,
            tenant_id=tenant_id,
        )

    if ctx:
        if ctx.state.hops >= effective_breaker.max_hops:
            raise CircuitBreakerException(
                reason=f"Maximum hop limit reached ({ctx.state.hops}/{effective_breaker.max_hops})",
                hops=ctx.state.hops,
                cost_usd=ctx.state.cost_usd,
                tokens=ctx.state.total_tokens,
                tenant_id=tenant_id,
            )
        if ctx.state.cost_usd >= effective_breaker.max_cost_usd:
            raise CircuitBreakerException(
                reason=f"Single-run hard cap reached (${ctx.state.cost_usd:.4f} / ${effective_breaker.max_cost_usd:.2f})",
                hops=ctx.state.hops,
                cost_usd=ctx.state.cost_usd,
                tokens=ctx.state.total_tokens,
                tenant_id=tenant_id,
            )

    executed_model = requested_model
    was_degraded = False

    if ctx and ctx.state.degraded_model:
        executed_model = ctx.state.degraded_model
        was_degraded = executed_model.lower() != requested_model.lower()
    elif ctx:
        step_down = ctx.state.maybe_activate_degradation(requested_model, effective_breaker)
        if step_down:
            executed_model = step_down
            was_degraded = True

    if not was_degraded and action == "degrade" and policy_model:
        executed_model = policy_model
        was_degraded = executed_model.lower() != requested_model.lower()
        if ctx and was_degraded:
            ctx.state.degraded_model = executed_model
            ctx.state.was_degraded = True
            ctx.state.add_step(
                step_type="degrade",
                title="Tenant Soft Cap Degradation",
                content=reason or f"Stepping down {requested_model} -> {executed_model}",
            )

    if was_degraded:
        call_kwargs["model"] = executed_model

    return call_kwargs, tenant_id, requested_model, executed_model, was_degraded, prompt_str, ctx


def _extract_anthropic_preview(response: Any) -> str:
    content = getattr(response, "content", None)
    if isinstance(content, (list, tuple)) and content:
        parts = []
        for block in content:
            txt = block.get("text") if isinstance(block, dict) else getattr(block, "text", None)
            if txt:
                parts.append(txt)
        return " ".join(parts)[:300]
    return ""


def _record_anthropic_result(
    pulse: Brenducate,
    agent_id: str,
    tenant_id: str,
    requested_model: str,
    executed_model: str,
    was_degraded: bool,
    prompt_str: str,
    content_preview: str,
    usage_obj: Any,
    latency_ms: int,
    ctx: Any,
) -> None:
    usage = extract_anthropic_usage(usage_obj)
    cost = calculate_cost(executed_model, usage)
    savings = calculate_degradation_savings(requested_model, executed_model, usage) if was_degraded else 0.0

    pulse._client.policy_store.record_local_spend(tenant_id, cost)

    if ctx:
        if not ctx.state.original_model:
            ctx.state.original_model = requested_model
        ctx.state.executed_model = executed_model
        if was_degraded:
            ctx.state.was_degraded = True

        ctx.state.record_hop(
            breaker=ctx.breaker,
            input_tokens=usage.input_tokens,
            cached_input_tokens=usage.cached_input_tokens,
            cache_creation_tokens=usage.cache_creation_tokens,
            output_tokens=usage.output_tokens,
            reasoning_tokens=usage.reasoning_tokens,
            cost_usd=cost,
            saved_cost_usd=savings,
            tool_call_str=f"messages.create(model='{executed_model}')",
            step_type="degrade" if was_degraded else "tool",
            title=f"Anthropic ({executed_model})" + (" [Degraded]" if was_degraded else ""),
            content=f"Prompt: {prompt_str[:120]}\nResponse: {content_preview[:120]}",
            tenant_id=tenant_id,
        )
        ctx.state.maybe_activate_degradation(requested_model, ctx.breaker)
    else:
        state = ExecutionState(
            hops=1,
            input_tokens=usage.input_tokens,
            cached_input_tokens=usage.cached_input_tokens,
            cache_creation_tokens=usage.cache_creation_tokens,
            output_tokens=usage.output_tokens,
            cost_usd=cost,
            saved_cost_usd=savings,
            original_model=requested_model,
            executed_model=executed_model,
            was_degraded=was_degraded,
        )
        state.add_step(step_type="thought", title="User Prompt", content=prompt_str)
        state.add_step(
            step_type="degrade" if was_degraded else "final",
            title=f"Anthropic Response ({executed_model})" + (" [Degraded]" if was_degraded else ""),
            content=content_preview,
        )
        status = "DEGRADED" if was_degraded else "COMPLETED"
        payload = {
            "tenant_id": tenant_id,
            "agent_id": agent_id,
            "original_model": requested_model,
            "executed_model": executed_model,
            "was_degraded": was_degraded,
            "prompt": prompt_str[:500] or "(no prompt captured)",
            "status": status,
            "input_tokens": state.input_tokens,
            "cached_input_tokens": state.cached_input_tokens,
            "cache_creation_tokens": state.cache_creation_tokens,
            "output_tokens": state.output_tokens,
            "reasoning_tokens": 0,
            "cost_usd": round(cost, 6),
            "estimated_savings_usd": round(savings, 6),
            "latency_ms": latency_ms,
            "hops": 1,
            "circuit_tripped": False,
            "guardrail_reason": None,
            "steps": state.steps,
            "environment": pulse.environment,
        }
        if pulse.verbose:
            print(
                f"[Brenducate] tenant='{tenant_id}' | agent='{agent_id}' | "
                f"model={executed_model} | cost=${cost:.4f} | saved=${savings:.4f}"
            )
        pulse._client.send_trace(payload)


def _accumulate_anthropic_event(event: Any, usage_acc: Dict[str, int], text_parts: list) -> None:
    etype = getattr(event, "type", None) or (event.get("type") if isinstance(event, dict) else None)
    if etype == "message_start":
        msg = getattr(event, "message", None) or (event.get("message") if isinstance(event, dict) else None)
        u = getattr(msg, "usage", None) if msg else None
        if u is not None:
            parsed = extract_anthropic_usage(u)
            usage_acc["input_tokens"] = max(usage_acc["input_tokens"], parsed.input_tokens)
            usage_acc["cached_read"] = max(usage_acc["cached_read"], parsed.cached_input_tokens)
            usage_acc["cache_create"] = max(usage_acc["cache_create"], parsed.cache_creation_tokens)
            usage_acc["output_tokens"] = max(usage_acc["output_tokens"], parsed.output_tokens)
    elif etype == "message_delta":
        u = getattr(event, "usage", None) or (event.get("usage") if isinstance(event, dict) else None)
        if u is not None:
            parsed = extract_anthropic_usage(u)
            if parsed.output_tokens:
                usage_acc["output_tokens"] = max(usage_acc["output_tokens"], parsed.output_tokens)
            if parsed.input_tokens:
                usage_acc["input_tokens"] = max(usage_acc["input_tokens"], parsed.input_tokens)
    elif etype == "content_block_delta":
        delta = getattr(event, "delta", None) or (event.get("delta") if isinstance(event, dict) else None)
        txt = getattr(delta, "text", None) if delta else None
        if txt and sum(len(x) for x in text_parts) < 300:
            text_parts.append(txt)


class _SyncAnthropicStream:
    """Proxy for both raw `messages.create(stream=True)` and `messages.stream(...)` context manager."""

    def __init__(self, target: Any, meta: Dict[str, Any], is_manager: bool = False) -> None:
        self._target = target
        self._stream = None if is_manager else target
        self._is_manager = is_manager
        self._meta = meta
        self._usage_acc = {"input_tokens": 0, "cached_read": 0, "cache_create": 0, "output_tokens": 0}
        self._text: list = []
        self._iter = None if is_manager else iter(target)
        self._finalized = False

    def __iter__(self):
        if self._iter is None and self._stream is not None:
            self._iter = iter(self._stream)
        return self

    def __next__(self):
        try:
            ev = next(self._iter)
            try:
                _accumulate_anthropic_event(ev, self._usage_acc, self._text)
            except Exception:
                pass
            return ev
        except StopIteration:
            self._finalize()
            raise

    def __enter__(self):
        self._stream = self._target.__enter__() if hasattr(self._target, "__enter__") else self._target
        self._iter = iter(self._stream) if hasattr(self._stream, "__iter__") else None
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        try:
            self._snapshot_from_stream()
            if hasattr(self._target, "__exit__"):
                self._target.__exit__(exc_type, exc_val, exc_tb)
        finally:
            self._finalize()
        return False

    def _snapshot_from_stream(self) -> None:
        if self._stream is None:
            return
        snap = getattr(self._stream, "current_message_snapshot", None)
        if snap is not None and getattr(snap, "usage", None) is not None:
            parsed = extract_anthropic_usage(snap.usage)
            self._usage_acc["input_tokens"] = max(self._usage_acc["input_tokens"], parsed.input_tokens)
            self._usage_acc["cached_read"] = max(self._usage_acc["cached_read"], parsed.cached_input_tokens)
            self._usage_acc["cache_create"] = max(self._usage_acc["cache_create"], parsed.cache_creation_tokens)
            self._usage_acc["output_tokens"] = max(self._usage_acc["output_tokens"], parsed.output_tokens)
            if not self._text:
                preview = _extract_anthropic_preview(snap)
                if preview:
                    self._text.append(preview)

    def _finalize(self) -> None:
        if self._finalized:
            return
        self._finalized = True
        latency_ms = int((time.monotonic() - self._meta["start_time"]) * 1000)
        usage_dict = {
            "input_tokens": self._usage_acc["input_tokens"],
            "cache_read_input_tokens": self._usage_acc["cached_read"],
            "cache_creation_input_tokens": self._usage_acc["cache_create"],
            "output_tokens": self._usage_acc["output_tokens"],
        }
        try:
            _record_anthropic_result(
                pulse=self._meta["pulse"],
                agent_id=self._meta["agent_id"],
                tenant_id=self._meta["tenant_id"],
                requested_model=self._meta["requested_model"],
                executed_model=self._meta["executed_model"],
                was_degraded=self._meta["was_degraded"],
                prompt_str=self._meta["prompt_str"],
                content_preview="".join(self._text),
                usage_obj=usage_dict,
                latency_ms=latency_ms,
                ctx=self._meta["ctx"],
            )
        except CircuitBreakerException:
            raise
        except Exception:
            pass

    def __getattr__(self, name: str) -> Any:
        target = self._stream if self._stream is not None else self._target
        return getattr(target, name)


class _AsyncAnthropicStream:
    """Async proxy for both `create(stream=True)` and `async with client.messages.stream(...)`."""

    def __init__(self, target: Any, meta: Dict[str, Any], is_manager: bool = False) -> None:
        self._target = target
        self._stream = None if is_manager else target
        self._is_manager = is_manager
        self._meta = meta
        self._usage_acc = {"input_tokens": 0, "cached_read": 0, "cache_create": 0, "output_tokens": 0}
        self._text: list = []
        self._aiter = None if is_manager else target.__aiter__()
        self._finalized = False

    def __aiter__(self):
        if self._aiter is None and self._stream is not None:
            self._aiter = self._stream.__aiter__()
        return self

    async def __anext__(self):
        try:
            ev = await self._aiter.__anext__()
            try:
                _accumulate_anthropic_event(ev, self._usage_acc, self._text)
            except Exception:
                pass
            return ev
        except StopAsyncIteration:
            self._finalize()
            raise

    async def __aenter__(self):
        self._stream = (
            await self._target.__aenter__() if hasattr(self._target, "__aenter__") else self._target
        )
        self._aiter = self._stream.__aiter__() if hasattr(self._stream, "__aiter__") else None
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        try:
            self._snapshot_from_stream()
            if hasattr(self._target, "__aexit__"):
                await self._target.__aexit__(exc_type, exc_val, exc_tb)
        finally:
            self._finalize()
        return False

    def _snapshot_from_stream(self) -> None:
        if self._stream is None:
            return
        snap = getattr(self._stream, "current_message_snapshot", None)
        if snap is not None and getattr(snap, "usage", None) is not None:
            parsed = extract_anthropic_usage(snap.usage)
            self._usage_acc["input_tokens"] = max(self._usage_acc["input_tokens"], parsed.input_tokens)
            self._usage_acc["cached_read"] = max(self._usage_acc["cached_read"], parsed.cached_input_tokens)
            self._usage_acc["cache_create"] = max(self._usage_acc["cache_create"], parsed.cache_creation_tokens)
            self._usage_acc["output_tokens"] = max(self._usage_acc["output_tokens"], parsed.output_tokens)
            if not self._text:
                preview = _extract_anthropic_preview(snap)
                if preview:
                    self._text.append(preview)

    def _finalize(self) -> None:
        if self._finalized:
            return
        self._finalized = True
        latency_ms = int((time.monotonic() - self._meta["start_time"]) * 1000)
        usage_dict = {
            "input_tokens": self._usage_acc["input_tokens"],
            "cache_read_input_tokens": self._usage_acc["cached_read"],
            "cache_creation_input_tokens": self._usage_acc["cache_create"],
            "output_tokens": self._usage_acc["output_tokens"],
        }
        try:
            _record_anthropic_result(
                pulse=self._meta["pulse"],
                agent_id=self._meta["agent_id"],
                tenant_id=self._meta["tenant_id"],
                requested_model=self._meta["requested_model"],
                executed_model=self._meta["executed_model"],
                was_degraded=self._meta["was_degraded"],
                prompt_str=self._meta["prompt_str"],
                content_preview="".join(self._text),
                usage_obj=usage_dict,
                latency_ms=latency_ms,
                ctx=self._meta["ctx"],
            )
        except CircuitBreakerException:
            raise
        except Exception:
            pass

    def __getattr__(self, name: str) -> Any:
        target = self._stream if self._stream is not None else self._target
        return getattr(target, name)


def _is_async_client(client: Any, method: Any) -> bool:
    if method is not None:
        if inspect.iscoroutinefunction(method):
            return True
        try:
            if inspect.iscoroutinefunction(inspect.unwrap(method)):
                return True
        except Exception:
            pass
    return any("async" in cls.__name__.lower() for cls in type(client).__mro__)


def wrap_anthropic(
    client: Any,
    pulse: Optional[Brenducate] = None,
    agent_id: str = "anthropic-agent",
    tenant_id: Optional[str] = None,
    circuit_breaker: Optional[CircuitBreaker] = None,
) -> Any:
    """
    Wrap an Anthropic or AsyncAnthropic client with Per-Tenant Margin Guardrails.
    Tracks additive cache read/write buckets and preserves prompt cache compatibility
    via same-family step-downs (e.g. Claude Sonnet -> Claude Haiku).
    """
    if pulse is None:
        pulse = Brenducate(
            api_key=os.getenv("BRENDUCATE_API_KEY", "br_live_demo_key_00001"),
            base_url=os.getenv("BRENDUCATE_BASE_URL", "https://brenducate.com"),
        )

    breaker = circuit_breaker or CircuitBreaker()

    if hasattr(client, "messages"):
        orig_create = getattr(client.messages, "create", None)
        orig_stream = getattr(client.messages, "stream", None)
        is_async = _is_async_client(client, orig_create)

        if orig_create is not None:
            if is_async:
                @functools.wraps(orig_create)
                async def wrapped_async_create(*args, **kwargs):
                    start_time = time.monotonic()
                    try:
                        prep = _prepare_anthropic_call(pulse, breaker, tenant_id, kwargs)
                    except CircuitBreakerException:
                        raise
                    except Exception:
                        return await orig_create(*args, **kwargs)

                    call_kwargs, tid, req_model, exec_model, degraded, prompt_str, ctx = prep
                    response = await orig_create(*args, **call_kwargs)

                    if call_kwargs.get("stream"):
                        return _AsyncAnthropicStream(
                            response,
                            {
                                "pulse": pulse,
                                "agent_id": agent_id,
                                "tenant_id": tid,
                                "requested_model": req_model,
                                "executed_model": exec_model,
                                "was_degraded": degraded,
                                "prompt_str": prompt_str,
                                "ctx": ctx,
                                "start_time": start_time,
                            },
                            is_manager=False,
                        )

                    latency_ms = int((time.monotonic() - start_time) * 1000)
                    try:
                        _record_anthropic_result(
                            pulse,
                            agent_id,
                            tid,
                            req_model,
                            exec_model,
                            degraded,
                            prompt_str,
                            _extract_anthropic_preview(response),
                            getattr(response, "usage", None),
                            latency_ms,
                            ctx,
                        )
                    except CircuitBreakerException:
                        raise
                    except Exception:
                        pass
                    return response

                client.messages.create = wrapped_async_create
            else:
                @functools.wraps(orig_create)
                def wrapped_sync_create(*args, **kwargs):
                    start_time = time.monotonic()
                    try:
                        prep = _prepare_anthropic_call(pulse, breaker, tenant_id, kwargs)
                    except CircuitBreakerException:
                        raise
                    except Exception:
                        return orig_create(*args, **kwargs)

                    call_kwargs, tid, req_model, exec_model, degraded, prompt_str, ctx = prep
                    response = orig_create(*args, **call_kwargs)

                    if call_kwargs.get("stream"):
                        return _SyncAnthropicStream(
                            response,
                            {
                                "pulse": pulse,
                                "agent_id": agent_id,
                                "tenant_id": tid,
                                "requested_model": req_model,
                                "executed_model": exec_model,
                                "was_degraded": degraded,
                                "prompt_str": prompt_str,
                                "ctx": ctx,
                                "start_time": start_time,
                            },
                            is_manager=False,
                        )

                    latency_ms = int((time.monotonic() - start_time) * 1000)
                    try:
                        _record_anthropic_result(
                            pulse,
                            agent_id,
                            tid,
                            req_model,
                            exec_model,
                            degraded,
                            prompt_str,
                            _extract_anthropic_preview(response),
                            getattr(response, "usage", None),
                            latency_ms,
                            ctx,
                        )
                    except CircuitBreakerException:
                        raise
                    except Exception:
                        pass
                    return response

                client.messages.create = wrapped_sync_create

        if orig_stream is not None:
            # `.stream(...)` returns a sync or async context manager directly (not a coroutine)
            @functools.wraps(orig_stream)
            def wrapped_stream(*args, **kwargs):
                start_time = time.monotonic()
                try:
                    prep = _prepare_anthropic_call(pulse, breaker, tenant_id, kwargs)
                except CircuitBreakerException:
                    raise
                except Exception:
                    return orig_stream(*args, **kwargs)

                call_kwargs, tid, req_model, exec_model, degraded, prompt_str, ctx = prep
                mgr = orig_stream(*args, **call_kwargs)
                meta = {
                    "pulse": pulse,
                    "agent_id": agent_id,
                    "tenant_id": tid,
                    "requested_model": req_model,
                    "executed_model": exec_model,
                    "was_degraded": degraded,
                    "prompt_str": prompt_str,
                    "ctx": ctx,
                    "start_time": start_time,
                }
                if is_async or (hasattr(mgr, "__aenter__") and not hasattr(mgr, "__enter__")):
                    return _AsyncAnthropicStream(mgr, meta, is_manager=True)
                return _SyncAnthropicStream(mgr, meta, is_manager=True)

            client.messages.stream = wrapped_stream

    return client
