"""
Brenducate SDK — OpenAI Sync & Async Client Auto-Instrumentation

Supports `OpenAI` and `AsyncOpenAI` (`chat.completions.create`, streaming and non-streaming).
- Automatic `stream_options={"include_usage": True}` injection on streaming requests
- Exact cached & reasoning token math
- Same-family Smart Degradation (e.g. gpt-4o -> gpt-4o-mini) in <1ms
- Strict fail-open safety on SDK internal errors
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
from ..pricing import calculate_cost, calculate_degradation_savings, extract_openai_usage


def _extract_tenant_header(kwargs: Dict[str, Any], default_tenant: Optional[str]) -> str:
    """Resolve tenant_id from header override -> active Scope/Trace -> default."""
    extra_headers = kwargs.get("extra_headers")
    if isinstance(extra_headers, dict):
        for k in list(extra_headers.keys()):
            if str(k).lower() == "x-brenducate-tenant":
                val = str(extra_headers[k]).strip()
                # Copy before mutating so caller's dict isn't modified unexpectedly
                copied = dict(extra_headers)
                copied.pop(k, None)
                kwargs["extra_headers"] = copied
                if val:
                    return val
    return Brenducate.get_current_tenant_id() or default_tenant or "default"


def _extract_prompt_preview(args: tuple, kwargs: Dict[str, Any]) -> str:
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
    resp_input = kwargs.get("input")
    if isinstance(resp_input, str):
        return resp_input[:500]
    if isinstance(resp_input, (list, tuple)) and resp_input:
        last_item = resp_input[-1]
        if isinstance(last_item, dict):
            return str(last_item.get("content", last_item))[:500]
        return str(getattr(last_item, "content", last_item))[:500]
    if args:
        return str(args[0])[:500]
    return ""


def _extract_response_preview(response: Any) -> str:
    if hasattr(response, "choices") and response.choices:
        msg = getattr(response.choices[0], "message", None)
        return (getattr(msg, "content", None) or "") if msg else ""
    out_txt = getattr(response, "output_text", None)
    if isinstance(out_txt, str):
        return out_txt[:300]
    return ""


def _build_tool_fingerprint(requested_model: str, kwargs: Dict[str, Any], prompt_str: str) -> str:
    return f"openai.chat.completions.create(model='{requested_model}', prompt='{prompt_str[:80]}')"


def _prepare_openai_call(
    pulse: Brenducate,
    breaker: CircuitBreaker,
    default_tenant: Optional[str],
    args: tuple,
    kwargs: Dict[str, Any],
    inject_stream_usage: bool = True,
) -> Tuple[Dict[str, Any], str, str, str, bool, Optional[str], Any]:
    """
    Evaluate pre-call guardrails and Smart Degradation in <1ms.
    Returns: (call_kwargs, tenant_id, requested_model, executed_model, was_degraded, prompt_str, ctx)
    """
    call_kwargs = dict(kwargs)
    tenant_id = _extract_tenant_header(call_kwargs, default_tenant)
    requested_model = call_kwargs.get("model", "gpt-4o")
    prompt_str = _extract_prompt_preview(args, call_kwargs)
    ctx = Brenducate.get_current_context()
    effective_breaker = pulse._client.policy_store.resolve_breaker(ctx.breaker if ctx else breaker)
    if ctx:
        ctx.breaker = effective_breaker

    # 1. Check synced tenant cap / block status
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

    # 2. Check active run hard limits
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

    # 3. Determine executed_model via sticky Smart Degradation
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

    # 4. Auto-inject stream_options={"include_usage": True} for chat.completions streams
    if inject_stream_usage and call_kwargs.get("stream"):
        stream_opts = dict(call_kwargs.get("stream_options") or {})
        stream_opts["include_usage"] = True
        call_kwargs["stream_options"] = stream_opts

    return call_kwargs, tenant_id, requested_model, executed_model, was_degraded, prompt_str, ctx


def _record_openai_result(
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
    api_label: str = "chat.completions.create",
) -> None:
    usage = extract_openai_usage(usage_obj)
    cost = calculate_cost(executed_model, usage)
    savings = calculate_degradation_savings(requested_model, executed_model, usage) if was_degraded else 0.0

    # Record optimistic local spend immediately so subsequent concurrent calls enforce caps
    pulse._client.policy_store.record_local_spend(tenant_id, cost)

    safe_prompt = pulse.redact_text(prompt_str)[:500] or "(no prompt captured)"
    safe_content = pulse.redact_text(content_preview, placeholder="[redacted]")[:300]
    step_detail = (
        "[redacted: metadata-only mode]"
        if not pulse.capture_prompts
        else f"Prompt: {safe_prompt[:120]}\nResponse: {safe_content[:120]}"
    )

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
            tool_call_str=f"{api_label}(model='{executed_model}')",
            step_type="degrade" if was_degraded else "tool",
            title=f"OpenAI ({executed_model})" + (" [Degraded]" if was_degraded else ""),
            content=step_detail,
            tenant_id=tenant_id,
        )
        # Ensure next hop checks soft cap cleanly
        ctx.state.maybe_activate_degradation(requested_model, ctx.breaker)
    else:
        state = ExecutionState(
            hops=1,
            input_tokens=usage.input_tokens,
            cached_input_tokens=usage.cached_input_tokens,
            cache_creation_tokens=usage.cache_creation_tokens,
            output_tokens=usage.output_tokens,
            reasoning_tokens=usage.reasoning_tokens,
            cost_usd=cost,
            saved_cost_usd=savings,
            original_model=requested_model,
            executed_model=executed_model,
            was_degraded=was_degraded,
        )
        state.add_step(step_type="thought", title="User Prompt", content=safe_prompt)
        state.add_step(
            step_type="degrade" if was_degraded else "final",
            title=f"OpenAI Response ({executed_model})" + (" [Degraded]" if was_degraded else ""),
            content=safe_content,
        )
        # Skip duplicate local spend increment since we recorded it above
        status = "DEGRADED" if was_degraded else "COMPLETED"
        payload = {
            "tenant_id": tenant_id,
            "agent_id": agent_id,
            "original_model": requested_model,
            "executed_model": executed_model,
            "was_degraded": was_degraded,
            "prompt": safe_prompt,
            "status": status,
            "input_tokens": state.input_tokens,
            "cached_input_tokens": state.cached_input_tokens,
            "cache_creation_tokens": state.cache_creation_tokens,
            "output_tokens": state.output_tokens,
            "reasoning_tokens": state.reasoning_tokens,
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


def _extract_chunk_delta_and_usage(chunk: Any, current_usage: Any, chunks_text: list) -> Any:
    usage = getattr(chunk, "usage", None)
    if usage is not None:
        current_usage = usage
    ev_type = getattr(chunk, "type", None)
    if ev_type == "response.completed":
        resp_obj = getattr(chunk, "response", None)
        resp_usage = getattr(resp_obj, "usage", None) if resp_obj is not None else None
        if resp_usage is not None:
            current_usage = resp_usage
    elif ev_type == "response.output_text.delta":
        delta_txt = getattr(chunk, "delta", None)
        if delta_txt and sum(len(x) for x in chunks_text) < 300:
            chunks_text.append(delta_txt)
    choices = getattr(chunk, "choices", None)
    if choices and len(choices) > 0:
        delta = getattr(choices[0], "delta", None)
        content = getattr(delta, "content", None) if delta is not None else None
        if content and sum(len(x) for x in chunks_text) < 300:
            chunks_text.append(content)
    return current_usage


class _SyncOpenAIStream:
    """Transparent sync stream proxy capturing final usage chunk without blocking yields."""

    def __init__(self, stream: Any, meta: Dict[str, Any]) -> None:
        self._stream = stream
        self._iter = iter(stream)
        self._meta = meta
        self._usage = None
        self._text: list = []
        self._finalized = False

    def __iter__(self):
        return self

    def __next__(self):
        try:
            chunk = next(self._iter)
            try:
                self._usage = _extract_chunk_delta_and_usage(chunk, self._usage, self._text)
            except Exception:
                pass
            return chunk
        except StopIteration:
            self._finalize()
            raise

    def __enter__(self):
        if hasattr(self._stream, "__enter__"):
            self._stream.__enter__()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        try:
            if hasattr(self._stream, "__exit__"):
                self._stream.__exit__(exc_type, exc_val, exc_tb)
        finally:
            self._finalize()
        return False

    def close(self):
        try:
            if hasattr(self._stream, "close"):
                self._stream.close()
        finally:
            self._finalize()

    def _finalize(self):
        if self._finalized:
            return
        self._finalized = True
        latency_ms = int((time.monotonic() - self._meta["start_time"]) * 1000)
        try:
            _record_openai_result(
                pulse=self._meta["pulse"],
                agent_id=self._meta["agent_id"],
                tenant_id=self._meta["tenant_id"],
                requested_model=self._meta["requested_model"],
                executed_model=self._meta["executed_model"],
                was_degraded=self._meta["was_degraded"],
                prompt_str=self._meta["prompt_str"],
                content_preview="".join(self._text),
                usage_obj=self._usage,
                latency_ms=latency_ms,
                ctx=self._meta["ctx"],
                api_label=self._meta.get("api_label", "chat.completions.create"),
            )
        except CircuitBreakerException:
            raise
        except Exception:
            pass

    def __getattr__(self, name: str) -> Any:
        return getattr(self._stream, name)


class _AsyncOpenAIStream:
    """Transparent async stream proxy capturing final usage chunk without blocking yields."""

    def __init__(self, stream: Any, meta: Dict[str, Any]) -> None:
        self._stream = stream
        self._aiter = stream.__aiter__()
        self._meta = meta
        self._usage = None
        self._text: list = []
        self._finalized = False

    def __aiter__(self):
        return self

    async def __anext__(self):
        try:
            chunk = await self._aiter.__anext__()
            try:
                self._usage = _extract_chunk_delta_and_usage(chunk, self._usage, self._text)
            except Exception:
                pass
            return chunk
        except StopAsyncIteration:
            self._finalize()
            raise

    async def __aenter__(self):
        if hasattr(self._stream, "__aenter__"):
            await self._stream.__aenter__()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        try:
            if hasattr(self._stream, "__aexit__"):
                await self._stream.__aexit__(exc_type, exc_val, exc_tb)
        finally:
            self._finalize()
        return False

    async def aclose(self):
        try:
            if hasattr(self._stream, "aclose"):
                await self._stream.aclose()
        finally:
            self._finalize()

    def _finalize(self):
        if self._finalized:
            return
        self._finalized = True
        latency_ms = int((time.monotonic() - self._meta["start_time"]) * 1000)
        try:
            _record_openai_result(
                pulse=self._meta["pulse"],
                agent_id=self._meta["agent_id"],
                tenant_id=self._meta["tenant_id"],
                requested_model=self._meta["requested_model"],
                executed_model=self._meta["executed_model"],
                was_degraded=self._meta["was_degraded"],
                prompt_str=self._meta["prompt_str"],
                content_preview="".join(self._text),
                usage_obj=self._usage,
                latency_ms=latency_ms,
                ctx=self._meta["ctx"],
                api_label=self._meta.get("api_label", "chat.completions.create"),
            )
        except CircuitBreakerException:
            raise
        except Exception:
            pass

    def __getattr__(self, name: str) -> Any:
        return getattr(self._stream, name)


def _is_async_client(client: Any, method: Any) -> bool:
    if inspect.iscoroutinefunction(method):
        return True
    try:
        if inspect.iscoroutinefunction(inspect.unwrap(method)):
            return True
    except Exception:
        pass
    return any("async" in cls.__name__.lower() for cls in type(client).__mro__)


def _patch_openai_endpoint(
    client: Any,
    resource: Any,
    pulse: Brenducate,
    agent_id: str,
    default_tenant: Optional[str],
    breaker: CircuitBreaker,
    inject_stream_usage: bool,
    api_label: str,
) -> None:
    """Wrap `resource.create` (sync or async) for Chat Completions or Responses API."""
    orig_create = getattr(resource, "create", None)
    if orig_create is None:
        return
    is_async = _is_async_client(client, orig_create)

    if is_async:
        @functools.wraps(orig_create)
        async def wrapped_async_create(*args, **kwargs):
            start_time = time.monotonic()
            try:
                prep = _prepare_openai_call(
                    pulse, breaker, default_tenant, args, kwargs, inject_stream_usage=inject_stream_usage
                )
            except CircuitBreakerException:
                raise
            except Exception:
                return await orig_create(*args, **kwargs)

            call_kwargs, tid, req_model, exec_model, degraded, prompt_str, ctx = prep

            try:
                response = await orig_create(*args, **call_kwargs)
            except Exception as e:
                if ctx:
                    try:
                        ctx.state.add_step(
                            step_type="guardrail",
                            title=f"OpenAI Error ({exec_model})",
                            content=str(e),
                        )
                    except Exception:
                        pass
                raise

            if call_kwargs.get("stream"):
                return _AsyncOpenAIStream(
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
                        "api_label": api_label,
                    },
                )

            latency_ms = int((time.monotonic() - start_time) * 1000)
            content_preview = _extract_response_preview(response)
            try:
                _record_openai_result(
                    pulse,
                    agent_id,
                    tid,
                    req_model,
                    exec_model,
                    degraded,
                    prompt_str,
                    content_preview,
                    getattr(response, "usage", None),
                    latency_ms,
                    ctx,
                    api_label=api_label,
                )
            except CircuitBreakerException:
                raise
            except Exception:
                pass
            return response

        resource.create = wrapped_async_create
    else:
        @functools.wraps(orig_create)
        def wrapped_sync_create(*args, **kwargs):
            start_time = time.monotonic()
            try:
                prep = _prepare_openai_call(
                    pulse, breaker, default_tenant, args, kwargs, inject_stream_usage=inject_stream_usage
                )
            except CircuitBreakerException:
                raise
            except Exception:
                return orig_create(*args, **kwargs)

            call_kwargs, tid, req_model, exec_model, degraded, prompt_str, ctx = prep

            try:
                response = orig_create(*args, **call_kwargs)
            except Exception as e:
                if ctx:
                    try:
                        ctx.state.add_step(
                            step_type="guardrail",
                            title=f"OpenAI Error ({exec_model})",
                            content=str(e),
                        )
                    except Exception:
                        pass
                raise

            if call_kwargs.get("stream"):
                return _SyncOpenAIStream(
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
                        "api_label": api_label,
                    },
                )

            latency_ms = int((time.monotonic() - start_time) * 1000)
            content_preview = _extract_response_preview(response)
            try:
                _record_openai_result(
                    pulse,
                    agent_id,
                    tid,
                    req_model,
                    exec_model,
                    degraded,
                    prompt_str,
                    content_preview,
                    getattr(response, "usage", None),
                    latency_ms,
                    ctx,
                    api_label=api_label,
                )
            except CircuitBreakerException:
                raise
            except Exception:
                pass
            return response

        resource.create = wrapped_sync_create


def wrap_openai(
    client: Any,
    pulse: Optional[Brenducate] = None,
    agent_id: str = "openai-agent",
    tenant_id: Optional[str] = None,
    circuit_breaker: Optional[CircuitBreaker] = None,
) -> Any:
    """
    Wrap an OpenAI or AsyncOpenAI client with 1-line Per-Tenant Margin Guardrails.
    Supports both `chat.completions.create` and `responses.create` (sync & async).
    Fail-open on SDK errors; auto-injects `stream_options={"include_usage": True}` on chat streams.
    """
    if pulse is None:
        pulse = Brenducate(
            api_key=os.getenv("BRENDUCATE_API_KEY", "br_live_demo_key_00001"),
            base_url=os.getenv("BRENDUCATE_BASE_URL", "https://www.brenducate.com"),
        )

    breaker = circuit_breaker or CircuitBreaker()

    if hasattr(client, "chat") and hasattr(client.chat, "completions"):
        _patch_openai_endpoint(
            client=client,
            resource=client.chat.completions,
            pulse=pulse,
            agent_id=agent_id,
            default_tenant=tenant_id,
            breaker=breaker,
            inject_stream_usage=True,
            api_label="chat.completions.create",
        )

    if hasattr(client, "responses"):
        _patch_openai_endpoint(
            client=client,
            resource=client.responses,
            pulse=pulse,
            agent_id=agent_id,
            default_tenant=tenant_id,
            breaker=breaker,
            inject_stream_usage=False,
            api_label="responses.create",
        )

    return client
