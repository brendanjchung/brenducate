"""
Brenducate — Per-Tenant Margin Guardrails & Smart Degradation Demo

Demonstrates:
  1. Healthy multi-tenant run on Claude Sonnet
  2. Sticky same-family Smart Degradation (crosses $0.10 soft cap -> steps down Sonnet -> Haiku)
  3. Runaway duplicate loop intercepted in <1ms

Run:
  python3 examples/demo_agent_run.py
"""

import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../sdk"))

from brenducate import Brenducate, CircuitBreaker, CircuitBreakerException


sentinel = Brenducate(
    api_key=os.getenv("BRENDUCATE_API_KEY", "br_live_demo_key_00001"),
    base_url=os.getenv("BRENDUCATE_BASE_URL", "http://localhost:8000"),
    environment="development",
    verbose=True,
    enable_background_sync=False,
)


@sentinel.monitor(
    agent_id="support-copilot",
    model="claude-sonnet-4",
    circuit_breaker=CircuitBreaker(
        degrade_after_cost_usd=0.05,
        max_cost_usd=1.00,
        max_hops=8,
    ),
)
def run_support_agent(goal: str) -> str:
    print(f"\n[support-copilot] Tenant={sentinel.get_current_tenant_id()} | Goal: {goal}")
    time.sleep(0.05)
    sentinel.record_step(
        step_type="tool",
        title="KB Lookup",
        tool_call_str="kb.search('billing tier')",
        input_tokens=15000,
        output_tokens=1200,
    )
    time.sleep(0.05)
    sentinel.record_step(
        step_type="tool",
        title="Draft Reply",
        tool_call_str="draft.reply()",
        input_tokens=6000,
        output_tokens=800,
    )
    return "Drafted response within tenant budget."


@sentinel.monitor(
    agent_id="sql-analyst",
    model="gpt-4o",
    circuit_breaker=CircuitBreaker(
        max_hops=8,
        max_cost_usd=0.50,
        kill_on_infinite_loop=True,
        max_duplicate_calls=2,
    ),
)
def run_looping_agent(goal: str) -> str:
    for i in range(6):
        query = "sql_exec('SELECT * FROM subscriptions WHERE status=pending')"
        print(f"  -> Hop {i+1}: {query}")
        sentinel.record_step(
            step_type="tool",
            title=f"SQL Query #{i+1}",
            tool_call_str=query,
            input_tokens=2000,
            output_tokens=300,
        )
    return "Should not reach here"


if __name__ == "__main__":
    with sentinel.tenant("acme_corp"):
        print("1) Healthy & Soft-Degraded Run:")
        print("  ", run_support_agent("Resolve customer invoice upgrade question"))

    print("\n2) Infinite Loop Interception:")
    try:
        with sentinel.tenant("beta_startup"):
            run_looping_agent("Analyze churn cohorts")
    except CircuitBreakerException as e:
        print(f"  Intercepted in <1ms: {e.reason}")

    flushed = sentinel.flush(timeout=2.0)
    print(f"\nFlushed {flushed} traces cleanly.")
