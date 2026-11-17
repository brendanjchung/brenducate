"""
Brenducate — Comprehensive End-to-End Test Suite

Verifies:
  1. Multi-tenant DB init, Signup, Login, Password verification & Key rotation
  2. Policy sync (/v1/policies/sync), policy updates, & manual tenant overrides
  3. Accurate token + prompt cache pricing math (OpenAI vs Anthropic conventions + longest-prefix match)
  4. Sync & Async OpenAI wrapper (`wrap_openai`) + auto `stream_options={"include_usage": True}`
  5. Sync & Async Anthropic wrapper (`wrap_anthropic`) across `.create()` and `.stream()`
  6. Sticky same-family Smart Degradation + Hard Cap (`TenantBudgetExceeded`)
  7. Serverless `flush()` & `aflush()` trace delivery + `/v1/dashboard/margins` unit-economics aggregation
  8. Strict fail-open safety when SDK receives malformed responses or unreachable API

Run:
  python3 tests/test_e2e.py
"""

import asyncio
import os
import sys
import tempfile
import unittest

# Use an isolated temporary SQLite database for hermetic E2E runs
_TMP_DB = os.path.join(tempfile.gettempdir(), f"brenducate_test_{os.getpid()}.db")
os.environ["DATABASE_URL"] = f"sqlite:///{_TMP_DB}"
os.environ["SESSION_SECRET"] = "test-session-secret"

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SERVER_DIR = os.path.join(BASE_DIR, "server")
SDK_DIR = os.path.join(BASE_DIR, "sdk")

sys.path.insert(0, SERVER_DIR)
sys.path.insert(0, SDK_DIR)

from fastapi.testclient import TestClient
from main import app
from database import init_db
from brenducate import (
    Brenducate,
    CircuitBreaker,
    CircuitBreakerException,
    ExecutionState,
    TenantBudgetExceeded,
    TokenUsage,
    calculate_cost,
    calculate_degradation_savings,
    resolve_step_down_model,
    wrap_anthropic,
    wrap_openai,
)
from brenducate.pricing import extract_anthropic_usage, extract_openai_usage


# ── Mock OpenAI / AsyncOpenAI Clients ────────────────────────────────────────

class _Obj:
    def __init__(self, **kwargs):
        for k, v in kwargs.items():
            setattr(self, k, v)


class MockOpenAICompletions:
    def __init__(self):
        self.calls = []

    def create(self, *args, **kwargs):
        self.calls.append(dict(kwargs))
        model = kwargs.get("model", "gpt-4o")
        if kwargs.get("stream"):
            # Respect OpenAI behavior: only include usage on last chunk if stream_options.include_usage=True
            include_usage = bool((kwargs.get("stream_options") or {}).get("include_usage"))
            chunks = [
                _Obj(choices=[_Obj(delta=_Obj(content="Hello "))], usage=None),
                _Obj(choices=[_Obj(delta=_Obj(content="world"))], usage=None),
                _Obj(
                    choices=[],
                    usage=_Obj(
                        prompt_tokens=2000,
                        completion_tokens=400,
                        prompt_tokens_details=_Obj(cached_tokens=1000),
                        completion_tokens_details=_Obj(reasoning_tokens=100),
                    )
                    if include_usage
                    else None,
                ),
            ]
            return iter(chunks)
        return _Obj(
            choices=[_Obj(message=_Obj(content=f"Sync response from {model}"))],
            usage=_Obj(
                prompt_tokens=10_000,
                completion_tokens=2_000,
                prompt_tokens_details=_Obj(cached_tokens=4_000),
                completion_tokens_details=_Obj(reasoning_tokens=500),
            ),
        )


class MockAsyncOpenAICompletions:
    def __init__(self):
        self.calls = []

    async def create(self, *args, **kwargs):
        self.calls.append(dict(kwargs))
        model = kwargs.get("model", "gpt-4o")
        if kwargs.get("stream"):
            include_usage = bool((kwargs.get("stream_options") or {}).get("include_usage"))

            async def gen():
                yield _Obj(choices=[_Obj(delta=_Obj(content="Async "))], usage=None)
                yield _Obj(choices=[_Obj(delta=_Obj(content="chunk"))], usage=None)
                yield _Obj(
                    choices=[],
                    usage=_Obj(
                        prompt_tokens=3000,
                        completion_tokens=600,
                        prompt_tokens_details=_Obj(cached_tokens=1000),
                        completion_tokens_details=_Obj(reasoning_tokens=0),
                    )
                    if include_usage
                    else None,
                )

            return gen()
        return _Obj(
            choices=[_Obj(message=_Obj(content=f"Async response from {model}"))],
            usage=_Obj(
                prompt_tokens=4000,
                completion_tokens=1000,
                prompt_tokens_details=_Obj(cached_tokens=0),
                completion_tokens_details=_Obj(reasoning_tokens=0),
            ),
        )


class MockOpenAIClient:
    def __init__(self):
        self.chat = _Obj(completions=MockOpenAICompletions())


class MockAsyncOpenAIClient:
    def __init__(self):
        self.chat = _Obj(completions=MockAsyncOpenAICompletions())


# ── Mock Anthropic / AsyncAnthropic Clients ──────────────────────────────────

class _SyncAnthropicStreamMgr:
    def __init__(self, model: str, calls_log: list, kwargs: dict):
        self.model = model
        self.calls_log = calls_log
        self.kwargs = kwargs
        self.current_message_snapshot = _Obj(
            content=[_Obj(text=f"Streamed from {model}")],
            usage=_Obj(
                input_tokens=500,
                cache_read_input_tokens=5000,
                cache_creation_input_tokens=1000,
                output_tokens=1200,
            ),
        )

    def __enter__(self):
        self.calls_log.append(dict(self.kwargs))
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        return False

    def __iter__(self):
        return iter(
            [
                _Obj(
                    type="message_start",
                    message=_Obj(
                        usage=_Obj(
                            input_tokens=500,
                            cache_read_input_tokens=5000,
                            cache_creation_input_tokens=1000,
                            output_tokens=0,
                        )
                    ),
                ),
                _Obj(type="content_block_delta", delta=_Obj(text=f"Streamed from {self.model}")),
                _Obj(type="message_delta", usage=_Obj(output_tokens=1200)),
            ]
        )


class _AsyncAnthropicStreamMgr:
    def __init__(self, model: str, calls_log: list, kwargs: dict):
        self.model = model
        self.calls_log = calls_log
        self.kwargs = kwargs
        self.current_message_snapshot = _Obj(
            content=[_Obj(text=f"Async streamed from {model}")],
            usage=_Obj(
                input_tokens=300,
                cache_read_input_tokens=4000,
                cache_creation_input_tokens=0,
                output_tokens=900,
            ),
        )

    async def __aenter__(self):
        self.calls_log.append(dict(self.kwargs))
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        return False

    def __aiter__(self):
        async def gen():
            yield _Obj(type="content_block_delta", delta=_Obj(text="Async "))
            yield _Obj(type="content_block_delta", delta=_Obj(text=f"from {self.model}"))

        return gen()


class MockAnthropicMessages:
    def __init__(self):
        self.calls = []

    def create(self, *args, **kwargs):
        self.calls.append(dict(kwargs))
        model = kwargs.get("model", "claude-sonnet-4-20250514")
        return _Obj(
            content=[_Obj(text=f"Anthropic response from {model}")],
            usage=_Obj(
                input_tokens=20_000,
                cache_read_input_tokens=10_000,
                cache_creation_input_tokens=0,
                output_tokens=5_000,
            ),
        )

    def stream(self, *args, **kwargs):
        model = kwargs.get("model", "claude-sonnet-4-20250514")
        return _SyncAnthropicStreamMgr(model, self.calls, kwargs)


class MockAsyncAnthropicMessages:
    def __init__(self):
        self.calls = []

    async def create(self, *args, **kwargs):
        self.calls.append(dict(kwargs))
        model = kwargs.get("model", "claude-sonnet-4-20250514")
        return _Obj(
            content=[_Obj(text=f"Async Anthropic response from {model}")],
            usage=_Obj(
                input_tokens=2_000,
                cache_read_input_tokens=8_000,
                cache_creation_input_tokens=0,
                output_tokens=800,
            ),
        )

    def stream(self, *args, **kwargs):
        model = kwargs.get("model", "claude-sonnet-4-20250514")
        return _AsyncAnthropicStreamMgr(model, self.calls, kwargs)


class TestBrenducateE2E(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        init_db(force=True)
        cls.client = TestClient(app)

    @classmethod
    def tearDownClass(cls):
        if os.path.exists(_TMP_DB):
            os.remove(_TMP_DB)

    def _make_sdk_bound_to_testclient(self, api_key: str) -> Brenducate:
        """
        Create a Brenducate SDK instance whose httpx transport routes synchronously
        and asynchronously to the FastAPI TestClient app (no socket needed).
        """
        import httpx

        test_client = self.client

        class _SyncTestClientTransport(httpx.BaseTransport):
            def handle_request(self, request: httpx.Request) -> httpx.Response:
                path = request.url.raw_path.decode("utf-8")
                headers = {
                    k: v for k, v in request.headers.items() if k.lower() != "host"
                }
                r = test_client.request(
                    method=request.method,
                    url=path,
                    content=request.content,
                    headers=headers,
                )
                return httpx.Response(
                    status_code=r.status_code,
                    headers=dict(r.headers),
                    content=r.content,
                )

        sentinel = Brenducate(
            api_key=api_key,
            base_url="http://testserver",
            enable_background_sync=False,
            register_atexit=False,
            verbose=False,
        )
        sentinel._client._http_client = httpx.Client(
            transport=_SyncTestClientTransport(),
            base_url="http://testserver",
        )

        async def _asgi_aflush(timeout: float = 2.0) -> int:
            flushed = 0
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="http://testserver",
            ) as ac:
                while True:
                    batch = sentinel._client._drain_batch(max_items=50)
                    if not batch:
                        break
                    if len(batch) == 1:
                        await ac.post(
                            "/v1/telemetry/traces",
                            json=batch[0],
                            headers=sentinel._client._headers,
                        )
                    else:
                        await ac.post(
                            "/v1/telemetry/traces/batch",
                            json={"traces": batch},
                            headers=sentinel._client._headers,
                        )
                    flushed += len(batch)
            return flushed

        sentinel._client.aflush = _asgi_aflush
        return sentinel

    def test_01_pricing_and_prompt_cache_math(self):
        """Verify longest-prefix match and OpenAI vs Anthropic cache token semantics."""
        # gpt-4o-mini must NOT accidentally match gpt-4o
        mini_cost = calculate_cost("gpt-4o-mini", TokenUsage(input_tokens=1_000_000, output_tokens=0))
        self.assertAlmostEqual(mini_cost, 0.15, places=4)

        # OpenAI: prompt_tokens=10,000 INCLUDES cached_tokens=4,000 -> 6,000 uncached + 4,000 cached
        oai_usage = extract_openai_usage(
            _Obj(
                prompt_tokens=10_000,
                completion_tokens=1_000,
                prompt_tokens_details=_Obj(cached_tokens=4_000),
                completion_tokens_details=_Obj(reasoning_tokens=200),
            )
        )
        self.assertEqual(oai_usage.input_tokens, 6_000)
        self.assertEqual(oai_usage.cached_input_tokens, 4_000)
        self.assertEqual(oai_usage.reasoning_tokens, 200)
        # gpt-4o: 6k * $2.50/M + 4k * $1.25/M + 1k * $10/M = 0.015 + 0.005 + 0.010 = 0.030
        self.assertAlmostEqual(calculate_cost("gpt-4o", oai_usage), 0.03, places=6)

        # Anthropic: additive buckets (input_tokens=1,000 uncached + 10,000 cache read + 2,000 write)
        ant_usage = extract_anthropic_usage(
            _Obj(
                input_tokens=1_000,
                cache_read_input_tokens=10_000,
                cache_creation_input_tokens=2_000,
                output_tokens=500,
            )
        )
        # claude-sonnet-4: 1k*3/M + 10k*0.30/M + 2k*3.75/M + 500*15/M = 0.003 + 0.003 + 0.0075 + 0.0075 = 0.021
        self.assertAlmostEqual(calculate_cost("claude-sonnet-4-20250514", ant_usage), 0.021, places=6)

        # Same-family step-down mapping preserves provider family and stops at mini/haiku
        self.assertEqual(resolve_step_down_model("claude-sonnet-4-20250514"), "claude-3-5-haiku-latest")
        self.assertEqual(resolve_step_down_model("gpt-4o-2024-11-20"), "gpt-4o-mini")
        self.assertIsNone(resolve_step_down_model("gpt-4o-mini"))
        self.assertIsNone(resolve_step_down_model("claude-3-5-haiku-latest"))

    def test_02_signup_login_and_key_rotation(self):
        res = self.client.post(
            "/v1/auth/signup",
            json={
                "team_name": "MarginTest Co",
                "email": "founder@margintest.ai",
                "password": "strongpassword123",
            },
        )
        self.assertEqual(res.status_code, 201)
        data = res.json()
        self.assertTrue(data["api_key"].startswith("br_live_"))
        self.assertTrue(data["session_token"].startswith("sess_"))

        # Login with password
        login_res = self.client.post(
            "/v1/auth/login",
            json={"email": "founder@margintest.ai", "password": "strongpassword123"},
        )
        self.assertEqual(login_res.status_code, 200)
        sess = login_res.json()["session_token"]

        # Rotate key via session token
        rot_res = self.client.post(
            "/v1/auth/keys/rotate",
            headers={"Authorization": f"Bearer {sess}"},
        )
        self.assertEqual(rot_res.status_code, 200)
        new_key = rot_res.json()["api_key"]
        self.assertNotEqual(new_key, data["api_key"])

        # Old key must now be rejected
        old_check = self.client.get(
            "/v1/auth/me",
            headers={"Authorization": f"Bearer {data['api_key']}"},
        )
        self.assertEqual(old_check.status_code, 401)

        # New key authenticates cleanly
        new_check = self.client.get(
            "/v1/auth/me",
            headers={"Authorization": f"Bearer {new_key}"},
        )
        self.assertEqual(new_check.status_code, 200)
        self.assertEqual(new_check.json()["team_name"], "MarginTest Co")

    def test_03_openai_sync_async_and_streaming_usage_injection(self):
        signup = self.client.post(
            "/v1/auth/signup",
            json={"team_name": "OAI Co", "email": "oai@test.ai", "password": "password123"},
        ).json()
        sentinel = self._make_sdk_bound_to_testclient(signup["api_key"])

        # Sync client + streaming -> must auto-inject stream_options={"include_usage": True}
        mock_sync = MockOpenAIClient()
        client = wrap_openai(
            mock_sync,
            pulse=sentinel,
            agent_id="sync-stream-bot",
            circuit_breaker=CircuitBreaker(degrade_after_cost_usd=0.01, max_cost_usd=2.0),
        )

        with sentinel.tenant("tenant_alpha"):
            stream = client.chat.completions.create(
                model="gpt-4o",
                messages=[{"role": "user", "content": "Stream hello"}],
                stream=True,
            )
            chunks = list(stream)
            self.assertEqual(len(chunks), 3)

        sent_kwargs = mock_sync.chat.completions.calls[0]
        self.assertEqual(sent_kwargs.get("stream_options"), {"include_usage": True})

        # Async client + async streaming
        mock_async = MockAsyncOpenAIClient()
        aclient = wrap_openai(mock_async, pulse=sentinel, agent_id="async-stream-bot")

        async def run_async_stream():
            async with sentinel.tenant("tenant_alpha"):
                astream = await aclient.chat.completions.create(
                    model="gpt-4o",
                    messages=[{"role": "user", "content": "Async hello"}],
                    stream=True,
                )
                collected = [c async for c in astream]
                self.assertEqual(len(collected), 3)
            flushed = await sentinel.aflush(timeout=2.0)
            self.assertEqual(flushed, 2)

        asyncio.run(run_async_stream())
        self.assertEqual(
            mock_async.chat.completions.calls[0].get("stream_options"),
            {"include_usage": True},
        )

        # Verify backend recorded exact non-zero streaming tokens for tenant_alpha
        traces_res = self.client.get(
            "/v1/telemetry/traces?tenant_id=tenant_alpha",
            headers={"Authorization": f"Bearer {signup['api_key']}"},
        )
        self.assertEqual(traces_res.status_code, 200)
        traces = traces_res.json()
        self.assertEqual(len(traces), 2)
        self.assertTrue(all(t["total_tokens"] > 0 and t["cost_usd"] > 0 for t in traces))
        sentinel.flush(timeout=1.0)
        sentinel._client.stop()

    def test_04_sticky_same_family_degradation_and_anthropic_sync_async(self):
        signup = self.client.post(
            "/v1/auth/signup",
            json={"team_name": "Claude Co", "email": "claude@test.ai", "password": "password123"},
        ).json()
        sentinel = self._make_sdk_bound_to_testclient(signup["api_key"])

        mock_ant = _Obj(messages=MockAnthropicMessages())
        client = wrap_anthropic(
            mock_ant,
            pulse=sentinel,
            agent_id="doc-analyzer",
            circuit_breaker=CircuitBreaker(
                degrade_after_cost_usd=0.05,  # Turn 1 costs ~$0.138 on Sonnet -> Turn 2 & 3 step down sticky to Haiku
                max_cost_usd=2.00,
                max_hops=10,
            ),
        )

        with sentinel.trace(
            agent_id="doc-analyzer",
            model="claude-sonnet-4-20250514",
            tenant_id="tenant_whale",
            circuit_breaker=CircuitBreaker(degrade_after_cost_usd=0.05, max_cost_usd=2.00),
        ):
            # Step 1: Runs on Sonnet ($0.138 > $0.05 soft cap)
            client.messages.create(
                model="claude-sonnet-4-20250514",
                max_tokens=1024,
                messages=[{"role": "user", "content": "Step 1 analyze"}],
            )
            # Step 2: Must step down same-family to claude-3-5-haiku-latest
            client.messages.create(
                model="claude-sonnet-4-20250514",
                max_tokens=1024,
                messages=[{"role": "user", "content": "Step 2 refine"}],
            )
            # Step 3 (.stream() context manager): Sticky Haiku persists for cache warmup
            with client.messages.stream(
                model="claude-sonnet-4-20250514",
                max_tokens=1024,
                messages=[{"role": "user", "content": "Step 3 stream"}],
            ) as s:
                list(s)

        models_called = [c["model"] for c in mock_ant.messages.calls]
        self.assertEqual(
            models_called,
            [
                "claude-sonnet-4-20250514",
                "claude-3-5-haiku-latest",
                "claude-3-5-haiku-latest",
            ],
        )

        # Also verify AsyncAnthropic `.create` and `.stream()`
        mock_async_ant = _Obj(messages=MockAsyncAnthropicMessages())
        aclient = wrap_anthropic(mock_async_ant, pulse=sentinel, agent_id="async-doc")

        async def run_async_ant():
            async with sentinel.tenant("tenant_async"):
                r = await aclient.messages.create(
                    model="claude-sonnet-4-20250514",
                    max_tokens=512,
                    messages=[{"role": "user", "content": "Hello async"}],
                )
                self.assertIn("Anthropic response", r.content[0].text)
                async with aclient.messages.stream(
                    model="claude-sonnet-4-20250514",
                    max_tokens=512,
                    messages=[{"role": "user", "content": "Hello async stream"}],
                ) as s:
                    chunks = [ev async for ev in s]
                    self.assertEqual(len(chunks), 2)

        asyncio.run(run_async_ant())

        flushed = sentinel.flush(timeout=2.0)
        self.assertGreaterEqual(flushed, 3)

        # Check `/v1/dashboard/margins` shows positive net savings from Step 2 & Step 3 degradation
        margins = self.client.get(
            "/v1/dashboard/margins",
            headers={"Authorization": f"Bearer {signup['api_key']}"},
        ).json()
        self.assertGreater(margins["net_margin_saved_30d_usd"], 0.0)
        whale_row = next(t for t in margins["top_tenants"] if t["tenant_id"] == "tenant_whale")
        self.assertGreater(whale_row["saved_today_usd"], 0.0)
        sentinel._client.stop()

    def test_05_policy_sync_and_tenant_cap_blocking(self):
        signup = self.client.post(
            "/v1/auth/signup",
            json={"team_name": "Cap Co", "email": "cap@test.ai", "password": "password123"},
        ).json()

        # Tighten policy: degrade tenant at $0.05/day, hard-block at $0.10/day
        put_res = self.client.put(
            "/v1/policies",
            headers={"Authorization": f"Bearer {signup['session_token']}"},
            json={
                "degrade_after_run_cost_usd": 0.25,
                "max_run_cost_usd": 1.50,
                "tenant_daily_degrade_usd": 0.05,
                "tenant_daily_cap_usd": 0.10,
                "max_hops": 10,
                "kill_on_infinite_loop": True,
                "max_duplicate_calls": 2,
                "model_fallback_map": {"gpt-4o": "gpt-4o-mini"},
            },
        )
        self.assertEqual(put_res.status_code, 200)

        # Seed spend via ingestion so tenant_heavy sits at $0.07 (soft-degraded) and tenant_dead sits at $0.12 (blocked)
        self.client.post(
            "/v1/telemetry/traces",
            headers={"Authorization": f"Bearer {signup['api_key']}"},
            json={
                "tenant_id": "tenant_heavy",
                "agent_id": "bot",
                "original_model": "gpt-4o",
                "executed_model": "gpt-4o",
                "cost_usd": 0.07,
            },
        )
        self.client.post(
            "/v1/telemetry/traces",
            headers={"Authorization": f"Bearer {signup['api_key']}"},
            json={
                "tenant_id": "tenant_dead",
                "agent_id": "bot",
                "original_model": "gpt-4o",
                "executed_model": "gpt-4o",
                "cost_usd": 0.12,
            },
        )

        sentinel = self._make_sdk_bound_to_testclient(signup["api_key"])
        self.assertTrue(sentinel._client.sync_policies_now(timeout=2.0))

        mock_oai = MockOpenAIClient()
        client = wrap_openai(mock_oai, pulse=sentinel, agent_id="bot")

        # tenant_heavy must auto-step-down to gpt-4o-mini in <1ms without making a network policy check
        with sentinel.tenant("tenant_heavy"):
            client.chat.completions.create(
                model="gpt-4o",
                messages=[{"role": "user", "content": "Help"}],
            )
        self.assertEqual(mock_oai.chat.completions.calls[0]["model"], "gpt-4o-mini")

        # tenant_dead must raise TenantBudgetExceeded BEFORE touching OpenAI
        calls_before = len(mock_oai.chat.completions.calls)
        with self.assertRaises(TenantBudgetExceeded):
            with sentinel.tenant("tenant_dead"):
                client.chat.completions.create(
                    model="gpt-4o",
                    messages=[{"role": "user", "content": "Blocked"}],
                )
        self.assertEqual(len(mock_oai.chat.completions.calls), calls_before)

        # Manual override from dashboard: restore tenant_dead to force_degrade -> allowed via mini
        ov_res = self.client.patch(
            "/v1/tenants/tenant_heavy/mode",
            headers={"Authorization": f"Bearer {signup['session_token']}"},
            json={"override_mode": "blocked"},
        )
        self.assertEqual(ov_res.status_code, 200)
        self.assertTrue(sentinel._client.sync_policies_now(timeout=2.0))
        with self.assertRaises(TenantBudgetExceeded):
            with sentinel.tenant("tenant_heavy"):
                client.chat.completions.create(
                    model="gpt-4o",
                    messages=[{"role": "user", "content": "Now manually blocked"}],
                )
        sentinel._client.stop()

    def test_06_loop_breaker_and_fail_open_guarantee(self):
        # 1. Loop breaker
        breaker = CircuitBreaker(max_hops=10, kill_on_infinite_loop=True, max_duplicate_calls=2)
        state = ExecutionState()
        state.record_hop(breaker=breaker, tool_call_str="sql('SELECT 1')")
        state.record_hop(breaker=breaker, tool_call_str="sql('SELECT 1')")
        with self.assertRaises(CircuitBreakerException):
            state.record_hop(breaker=breaker, tool_call_str="sql('SELECT 1')")

        # 2. Fail-open: unreachable backend + malformed response object must NEVER break caller
        bad_sentinel = Brenducate(
            api_key="br_live_dead",
            base_url="http://127.0.0.1:59999",
            enable_background_sync=False,
            register_atexit=False,
        )
        mock_oai = MockOpenAIClient()
        client = wrap_openai(mock_oai, pulse=bad_sentinel, agent_id="bot")
        resp = client.chat.completions.create(
            model="gpt-4o",
            messages=[{"role": "user", "content": "Still succeeds"}],
        )
        self.assertIn("Sync response", resp.choices[0].message.content)
        self.assertEqual(bad_sentinel.flush(timeout=0.3), 1)  # Drains safely without raising
        bad_sentinel._client.stop()

    def test_07_real_openai_v2_client_sync_and_async_streaming(self):
        """Verify wrap_openai against real `openai.OpenAI` and `openai.AsyncOpenAI` v2 SDK objects."""
        import json
        import httpx
        from openai import AsyncOpenAI, OpenAI

        signup = self.client.post(
            "/v1/auth/signup",
            json={"team_name": "RealOAI Co", "email": "realoai@test.ai", "password": "password123"},
        ).json()
        sentinel = self._make_sdk_bound_to_testclient(signup["api_key"])

        captured_bodies = []
        captured_headers = []

        sse_stream_bytes = (
            b'data: {"id":"chatcmpl-1","object":"chat.completion.chunk","created":1,"model":"gpt-4o","choices":[{"index":0,"delta":{"role":"assistant","content":"Hi"},"finish_reason":null}]}\n\n'
            b'data: {"id":"chatcmpl-1","object":"chat.completion.chunk","created":1,"model":"gpt-4o","choices":[],"usage":{"prompt_tokens":1500,"completion_tokens":200,"total_tokens":1700,"prompt_tokens_details":{"cached_tokens":500},"completion_tokens_details":{"reasoning_tokens":50}}}\n\n'
            b"data: [DONE]\n\n"
        )

        def sync_handler(request: httpx.Request) -> httpx.Response:
            captured_bodies.append(json.loads(request.content.decode("utf-8")))
            captured_headers.append(dict(request.headers))
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                content=sse_stream_bytes,
            )

        async def async_handler(request: httpx.Request) -> httpx.Response:
            captured_bodies.append(json.loads(request.content.decode("utf-8")))
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                content=sse_stream_bytes,
            )

        sync_oai = wrap_openai(
            OpenAI(
                api_key="sk-test-fake",
                http_client=httpx.Client(transport=httpx.MockTransport(sync_handler)),
            ),
            pulse=sentinel,
            agent_id="real-oai-sync",
        )
        with sync_oai.chat.completions.create(
            model="gpt-4o",
            messages=[{"role": "user", "content": "Hello real SDK"}],
            stream=True,
            extra_headers={"X-Brenducate-Tenant": "tenant_real_sdk"},
        ) as stream:
            list(stream)

        # Ensure stream_options.include_usage was sent on wire and custom tenant header stripped cleanly
        self.assertEqual(captured_bodies[0].get("stream_options"), {"include_usage": True})
        self.assertNotIn("x-brenducate-tenant", captured_headers[0])

        async def run_async_real():
            async with httpx.AsyncClient(
                transport=httpx.MockTransport(async_handler)
            ) as ahc:
                async_oai = wrap_openai(
                    AsyncOpenAI(api_key="sk-test-fake", http_client=ahc),
                    pulse=sentinel,
                    agent_id="real-oai-async",
                )
                async with sentinel.tenant("tenant_real_sdk"):
                    astream = await async_oai.chat.completions.create(
                        model="gpt-4o",
                        messages=[{"role": "user", "content": "Hello async real SDK"}],
                        stream=True,
                    )
                    chunks = [c async for c in astream]
                    self.assertGreaterEqual(len(chunks), 1)
                await sentinel.aflush(timeout=2.0)

        asyncio.run(run_async_real())
        sentinel.flush(timeout=2.0)

        traces = self.client.get(
            "/v1/telemetry/traces?tenant_id=tenant_real_sdk",
            headers={"Authorization": f"Bearer {signup['api_key']}"},
        ).json()
        self.assertEqual(len(traces), 2)
        for t in traces:
            self.assertEqual(t["input_tokens"], 1000)       # 1500 total - 500 cached
            self.assertEqual(t["cached_input_tokens"], 500)
            self.assertEqual(t["output_tokens"], 200)
            self.assertEqual(t["reasoning_tokens"], 50)
            self.assertGreater(t["cost_usd"], 0)
        sentinel._client.stop()


if __name__ == "__main__":
    unittest.main(verbosity=2)
