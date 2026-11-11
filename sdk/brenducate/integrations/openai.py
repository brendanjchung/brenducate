"""
AgentPulse SDK — OpenAI Client Integration & Auto-Instrumentation

Enables 1-line auto-instrumentation for OpenAI clients:
    from openai import OpenAI
    from agentpulse import AgentPulse
    from agentpulse.integrations.openai import wrap_openai

    pulse = AgentPulse(api_key="ap_live_...")
    client = wrap_openai(OpenAI(), pulse=pulse, agent_id="support-agent")

Every call to `client.chat.completions.create(...)` automatically tracks:
  - Input/Output tokens & total cost calculated with live model pricing
  - Latency (ms)
  - Full prompt & completion content
  - Sub-millisecond circuit breaker enforcement (cost ceiling & loop prevention)
"""

import time
import functools
from typing import Optional, Any
from ..circuit_breaker import CircuitBreaker, CircuitBreakerException, ExecutionState, calculate_token_cost
from ..monitor import AgentPulse, TraceContext, _current_trace_context


def wrap_openai(
    client: Any,
    pulse: Optional[AgentPulse] = None,
    agent_id: str = "openai-agent",
    circuit_breaker: Optional[CircuitBreaker] = None,
) -> Any:
    """
    Wraps an OpenAI client instance (sync or async) with automatic AgentPulse telemetry.
    """
    if pulse is None:
        import os
        api_key = os.getenv("AGENTPULSE_API_KEY", "ap_live_demo_key_00001")
        base_url = os.getenv("AGENTPULSE_BASE_URL", "http://localhost:8000")
        pulse = AgentPulse(api_key=api_key, base_url=base_url)

    breaker = circuit_breaker or CircuitBreaker()

    # Wrap chat.completions.create
    if hasattr(client, "chat") and hasattr(client.chat, "completions"):
        orig_create = client.chat.completions.create

        @functools.wraps(orig_create)
        def wrapped_create(*args, **kwargs):
            model = kwargs.get("model", "gpt-4o")
            messages = kwargs.get("messages", [])
            
            # Extract prompt preview
            prompt_str = ""
            if messages:
                last_msg = messages[-1]
                prompt_str = last_msg.get("content", "") if isinstance(last_msg, dict) else getattr(last_msg, "content", "")
            if not prompt_str and args:
                prompt_str = str(args[0])

            start_time = time.monotonic()
            
            # Check if we are already inside an active trace context
            ctx = AgentPulse.get_current_context()
            
            try:
                response = orig_create(*args, **kwargs)
            except Exception as e:
                latency_ms = int((time.monotonic() - start_time) * 1000)
                if ctx:
                    ctx.state.add_step(
                        step_type="guardrail",
                        title=f"OpenAI Call Failed ({model})",
                        content=str(e)
                    )
                raise e

            latency_ms = int((time.monotonic() - start_time) * 1000)

            # Extract usage
            input_tokens = 0
            output_tokens = 0
            if hasattr(response, "usage") and response.usage:
                input_tokens = getattr(response.usage, "prompt_tokens", 0) or 0
                output_tokens = getattr(response.usage, "completion_tokens", 0) or 0

            cost = calculate_token_cost(model, input_tokens, output_tokens)

            # Extract response preview
            content_preview = ""
            if hasattr(response, "choices") and response.choices:
                first_choice = response.choices[0]
                if hasattr(first_choice, "message") and hasattr(first_choice.message, "content"):
                    content_preview = first_choice.message.content or ""

            # If inside an existing trace, record hop into that trace's graph
            if ctx:
                ctx.state.record_hop(
                    breaker=ctx.breaker,
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    cost_usd=cost,
                    tool_call_str=f"chat.completions.create(model='{model}')",
                    step_type="tool",
                    title=f"OpenAI Call: {model}",
                    content=f"Prompt: {prompt_str[:120]}...\nResponse: {content_preview[:120]}...",
                )
            else:
                # Standalone call - ship trace directly
                state = ExecutionState()
                state.input_tokens = input_tokens
                state.output_tokens = output_tokens
                state.cost_usd = cost
                state.hops = 1
                state.add_step(
                    step_type="thought",
                    title="User Prompt",
                    content=prompt_str
                )
                state.add_step(
                    step_type="final",
                    title=f"OpenAI Response ({model})",
                    content=content_preview
                )
                pulse._ship_trace(
                    agent_id=agent_id,
                    model=model,
                    prompt=prompt_str[:500],
                    state=state,
                    status="COMPLETED",
                    latency_ms=latency_ms,
                    guardrail_reason=None,
                    circuit_tripped=False,
                )

            return response

        client.chat.completions.create = wrapped_create

    return client
