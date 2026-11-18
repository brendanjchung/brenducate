# Brenducate Python SDK (`brenducate-sdk`)

Lightweight, open-source **Per-Tenant Margin Guardrails**, **Same-Family Smart Degradation**, and **Sub-Millisecond Loop Interception** for **OpenAI** and **Anthropic** (`OpenAI`, `AsyncOpenAI`, `Anthropic`, `AsyncAnthropic`).

- **Zero critical-path network latency (`<1ms`):** All run caps, loop detection, and tenant spend checks run in-process against synced dashboard policies.
- **Same-Family Smart Degradation:** When a run or end-customer crosses your soft budget threshold, calls automatically step down within the same provider family (`claude-sonnet-4` → `claude-3-5-haiku-latest`, `gpt-4o` → `gpt-4o-mini`, `gpt-5` → `gpt-5-mini`) and stay sticky per run to preserve prompt cache hits.
- **Exact Streaming & Prompt Cache Math:** Supports OpenAI `chat.completions` and `responses.create` (auto-injecting `stream_options={"include_usage": True}` on chat streams) plus Anthropic `.create()` & `.stream()`, accurately separating cached reads, cache writes, and reasoning tokens.
- **Metadata-Only Privacy Option (`capture_prompts=False`):** Strip prompt and response bodies before telemetry leaves your box—reporting only token counts, model names, latency, and USD spend.
- **Serverless Safe & Fail-Open:** Background queue flushes cleanly via `flush()` / `await aflush()` and `atexit` hooks for AWS Lambda & Modal. SDK internal errors never block LLM calls.

## Install

```bash
pip install brenducate-sdk
# or from source:
pip install -e sdk/
```

## Quickstart: Per-Tenant Guardrails (OpenAI & Anthropic)

```python
from openai import OpenAI
from brenducate import Brenducate, CircuitBreaker, wrap_openai

sentinel = Brenducate(api_key="br_live_...")

client = wrap_openai(
    OpenAI(),
    pulse=sentinel,
    agent_id="support-copilot",
    circuit_breaker=CircuitBreaker(
        degrade_after_cost_usd=0.25,  # Step down gpt-4o -> gpt-4o-mini after $0.25/run
        max_cost_usd=1.50,            # Hard kill run at $1.50
        max_hops=12,
    ),
)

with sentinel.tenant("acme_corp"):
    resp = client.chat.completions.create(
        model="gpt-4o",
        messages=[{"role": "user", "content": "Analyze Q3 support tickets"}],
        stream=True,  # stream_options={"include_usage": True} injected automatically
    )
    for chunk in resp:
        pass
```

Works identically with `AsyncOpenAI`, `Anthropic`, and `AsyncAnthropic` (including `.messages.create` and `.messages.stream`):

```python
from anthropic import AsyncAnthropic
from brenducate import wrap_anthropic

anthropic_client = wrap_anthropic(AsyncAnthropic(), pulse=sentinel)

async with sentinel.tenant("acme_corp"):
    async with anthropic_client.messages.stream(
        model="claude-sonnet-4-20250514",
        max_tokens=1024,
        messages=[{"role": "user", "content": "Draft onboarding plan"}],
    ) as stream:
        async for text in stream.text_stream:
            pass
```

## Serverless Environments (AWS Lambda / Modal)

Call `sentinel.flush()` before returning your handler response so queued telemetry always flushes before container freeze:

```python
def lambda_handler(event, context):
    try:
        with sentinel.tenant(event["tenant_id"]):
            return run_agent(event["prompt"])
    finally:
        sentinel.flush(timeout=2.0)
```
