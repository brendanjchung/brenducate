<div align="center">
  <h1>Brenducate</h1>
  <p><strong>Per-Tenant Margin Guardrails, Same-Family Smart Degradation &amp; Sub-Millisecond Loop Interception for AI Products</strong></p>
</div>

---

## What Brenducate Does

1. **Per-Tenant Unit Economics:** Attribute every LLM request and token to an end-customer (`with sentinel.tenant("acme_corp"):`). Spot unprofitable power-users immediately.
2. **Same-Family Smart Degradation:** Automatically step heavy runs or high-spend tenants down within the same provider family (`claude-sonnet-4` → `claude-3-5-haiku-latest`, `gpt-4o` → `gpt-4o-mini`) while keeping tool schemas and prompt caches intact.
3. **Sub-Millisecond In-Process Breakers:** Kill infinite duplicate tool loops and hard spend overruns in memory (`<1ms`) before the next API request leaves your box.
4. **OpenAI &amp; Anthropic Exact Metering:** Wraps `OpenAI`, `AsyncOpenAI`, `Anthropic`, and `AsyncAnthropic`, injects `stream_options={"include_usage": True}` automatically on OpenAI streams, and separates uncached, cached-read, cache-write, and reasoning tokens.
5. **Serverless Safe &amp; Fail-Open:** Background batch queue + `flush()` / `atexit` hooks for AWS Lambda and Modal. SDK errors never crash production calls.

---

## Quick Start

```bash
# Run FastAPI backend (port 8000) & dashboard + landing page (port 3032)
python3 start.py
```

- **Landing Page:** http://localhost:3032/index.html
- **Margin Control Dashboard:** http://localhost:3032/dashboard.html
- **Telemetry API Docs:** http://localhost:8000/docs

---

## Open-Source Python SDK (`sdk/`)

```bash
pip install brenducate-sdk
# or locally:
pip install -e sdk/
```

```python
from openai import OpenAI
from brenducate import Brenducate, CircuitBreaker, wrap_openai

sentinel = Brenducate(api_key="br_live_...")
client = wrap_openai(
    OpenAI(),
    pulse=sentinel,
    circuit_breaker=CircuitBreaker(degrade_after_cost_usd=0.25, max_cost_usd=1.50),
)

with sentinel.tenant("acme_corp"):
    resp = client.chat.completions.create(
        model="gpt-4o",
        messages=[{"role": "user", "content": "Analyze quarterly support trends"}],
        stream=True,
    )
    for chunk in resp:
        pass
```

---

## Automated Tests

```bash
python3 tests/test_e2e.py
```

---

## Production Deployment (Zero-Budget Free Tier)

- **Database:** Paste a free-tier PostgreSQL `DATABASE_URL` from Neon or Supabase plus `SESSION_SECRET` into Vercel / Render environment variables.
- **Deploy:** Connect repo to Vercel (`vercel.json` serves static pages + FastAPI backend on `/v1/*`).
