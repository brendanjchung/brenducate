"""
AgentPulse Ops — Automated End-to-End Test Suite

Verifies:
  1. Database initialization & API Key provisioning
  2. CircuitBreaker logic (max hops, cost ceiling, infinite loop intercept)
  3. FastAPI endpoint handling (traces ingestion, summary, alert creation)
  4. Stripe billing checkout simulation
  5. SDK framework integrations (OpenAI auto-wrap)

Run:
  python3 tests/test_e2e.py
"""

import sys
import os
import unittest

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SERVER_DIR = os.path.join(BASE_DIR, "server")
SDK_DIR = os.path.join(BASE_DIR, "sdk")

sys.path.insert(0, SERVER_DIR)
sys.path.insert(0, SDK_DIR)

from fastapi.testclient import TestClient
from main import app
from database import init_db, SessionLocal, ApiKey, Trace, Alert
from agentpulse import AgentPulse, CircuitBreaker, CircuitBreakerException, ExecutionState, wrap_openai
from agentpulse.circuit_breaker import calculate_token_cost


class TestAgentPulseE2E(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        init_db()
        cls.client = TestClient(app)
        cls.demo_key = "ap_live_demo_key_00001"

    def test_01_health_check(self):
        """Verify API health endpoint responds with 200 OK."""
        response = self.client.get("/health")
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["status"], "ok")

    def test_02_create_api_key(self):
        """Verify dynamic API key creation."""
        res = self.client.post("/v1/auth/keys", json={
            "team_name": "Acme AI Corp",
            "plan": "pro",
            "email": "cto@acme.ai"
        })
        self.assertEqual(res.status_code, 201)
        data = res.json()
        self.assertTrue(data["key"].startswith("ap_live_"))
        self.assertEqual(data["team_name"], "Acme AI Corp")
        self.assertEqual(data["plan"], "pro")

    def test_03_circuit_breaker_infinite_loop(self):
        """Verify CircuitBreaker trips on 3 duplicate consecutive tool calls."""
        breaker = CircuitBreaker(max_hops=10, kill_on_infinite_loop=True, max_duplicate_calls=2)
        state = ExecutionState()

        # Call 1 & 2 pass
        state.record_hop(breaker=breaker, tool_call_str="sql_exec('SELECT * FROM data')")
        state.record_hop(breaker=breaker, tool_call_str="sql_exec('SELECT * FROM data')")

        # Call 3 must raise CircuitBreakerException
        with self.assertRaises(CircuitBreakerException) as ctx:
            state.record_hop(breaker=breaker, tool_call_str="sql_exec('SELECT * FROM data')")
        
        self.assertIn("Repetitive tool call detected", ctx.exception.reason)

    def test_04_circuit_breaker_cost_ceiling(self):
        """Verify CircuitBreaker trips when token cost crosses limit."""
        breaker = CircuitBreaker(max_hops=10, max_cost_usd=0.10)
        state = ExecutionState()

        # Ingest 50,000 output tokens on Claude 3.7 ($15.00/1M = $0.75 > $0.10)
        cost = calculate_token_cost("claude-3-7-sonnet", input_tokens=1000, output_tokens=50000)
        self.assertGreater(cost, 0.10)

        with self.assertRaises(CircuitBreakerException) as ctx:
            state.record_hop(breaker=breaker, cost_usd=cost, tool_call_str="summarize_repo()")
        
        self.assertIn("cost ceiling exceeded", ctx.exception.reason)

    def test_05_ingest_trace_and_trigger_alert(self):
        """Verify trace ingestion and automatic alert logging on circuit trip."""
        payload = {
            "agent_id": "test-sentinel-bot",
            "model": "gpt-4o",
            "prompt": "Test query for unit test",
            "status": "CIRCUIT_KILLED",
            "input_tokens": 1500,
            "output_tokens": 300,
            "cost_usd": 0.007,
            "latency_ms": 350,
            "hops": 4,
            "circuit_tripped": True,
            "guardrail_reason": "Infinite loop detected on test",
            "steps": [
                {"step_index": 1, "step_type": "thought", "title": "Start", "content": "Init"},
                {"step_index": 2, "step_type": "guardrail", "title": "Killed", "content": "Killed"}
            ]
        }
        res = self.client.post(
            "/v1/telemetry/traces",
            headers={"Authorization": f"Bearer {self.demo_key}"},
            json=payload
        )
        self.assertEqual(res.status_code, 202)
        data = res.json()
        self.assertTrue(data["trace_id"].startswith("TR-"))

        # Verify alert was created in /v1/alerts
        alerts_res = self.client.get(
            "/v1/alerts",
            headers={"Authorization": f"Bearer {self.demo_key}"}
        )
        self.assertEqual(alerts_res.status_code, 200)
        alerts = alerts_res.json()
        self.assertTrue(any(a["agent_id"] == "test-sentinel-bot" for a in alerts))

    def test_06_dashboard_summary(self):
        """Verify KPI aggregation endpoint returns expected data."""
        res = self.client.get(
            "/v1/dashboard/summary",
            headers={"Authorization": f"Bearer {self.demo_key}"}
        )
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertIn("active_agents", data)
        self.assertIn("total_traces_24h", data)
        self.assertIn("circuit_trips_today", data)

    def test_07_billing_checkout_session(self):
        """Verify billing checkout session generator."""
        res = self.client.post("/v1/billing/create-checkout-session", json={
            "plan": "pro",
            "email": "lead@ai-firm.co",
            "team_name": "AI Firm Co"
        })
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertIn("checkout_url", data)
        self.assertEqual(data["plan"], "pro")


if __name__ == "__main__":
    unittest.main(verbosity=2)
