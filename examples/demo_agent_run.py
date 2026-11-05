"""
AgentPulse Ops — Live Telemetry & Circuit Breaker Demonstration

Shows realistic agent executions and guardrail interceptions:
  1. Healthy Multi-Hop Research Agent (Succeeds with accurate tokens & cost)
  2. Rogue Loop Agent (Aborted by Infinite Recursion Sentinel)
  3. Context Explosion Agent (Aborted by Single-Run Cost Ceiling Guardrail)

Run:
  python3 examples/demo_agent_run.py
"""

import sys
import os
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../sdk"))

from agentpulse import AgentPulse, CircuitBreaker, CircuitBreakerException

# ── 1. Initialise the Sentinel ────────────────────────────────────────────────

pulse = AgentPulse(
    api_key="ap_live_demo_key_00001",
    base_url="http://localhost:8000",
    environment="production",
    verbose=True,
)

# ── Example 1: Healthy Agent Run (completes normally with full telemetry) ─────

@pulse.monitor(
    agent_id="financial-research-bot",
    model="claude-3-7-sonnet",
    circuit_breaker=CircuitBreaker(max_hops=8, max_cost_usd=1.00)
)
def run_financial_research(prompt: str) -> str:
    """Simulates a multi-hop research agent recording steps."""
    print(f"\n📋 [Agent: financial-research-bot] Running objective: '{prompt[:60]}...'")

    # Step 1: Query decomposition
    time.sleep(0.15)
    pulse.record_step(
        step_type="thought",
        title="Query Decomposition",
        content="Parsing query targets: NVIDIA Q3 2026 10-Q filing, data center margins.",
        input_tokens=1400,
        output_tokens=320,
    )

    # Step 2: SEC EDGAR Tool Call
    time.sleep(0.2)
    pulse.record_step(
        step_type="tool",
        title="Tool Call: sec_edgar.fetch()",
        content="Fetched Form 10-Q filed Oct 2026. Extracted Data Center revenue segment.",
        tool_call_str='sec_edgar.fetch(ticker="NVDA", form="10-Q")',
        input_tokens=4800,
        output_tokens=450,
    )

    # Step 3: Vector search for margin guidance
    time.sleep(0.15)
    pulse.record_step(
        step_type="tool",
        title="Tool Call: vector_search()",
        content="Retrieved 3 chunks: Data Center gross margin reached 75.1%.",
        tool_call_str='vector_search(query="gross margin guidance Blackwell ramp", top_k=3)',
        input_tokens=3200,
        output_tokens=280,
    )

    return "Analysis complete: NVDA gross margin 74.6% in Q3 2026, beat consensus by 2.1pp."


# ── Example 2: Rogue Agent Trapped in Infinite Recursion ──────────────────────

@pulse.monitor(
    agent_id="sql-analyst-bot",
    model="gpt-4o",
    circuit_breaker=CircuitBreaker(
        max_hops=6,
        max_cost_usd=0.50,
        kill_on_infinite_loop=True,
        max_duplicate_calls=2,
    )
)
def run_sql_analyst_infinite_loop(prompt: str) -> str:
    """Simulates a rogue agent repeating the exact same failing query."""
    print(f"\n💀 [Agent: sql-analyst-bot] Starting rogue SQL execution...")

    for i in range(10):
        time.sleep(0.1)
        query = "sql_exec('SELECT * FROM subscriptions WHERE status=pending')"
        print(f"  ↳ Hop {i+1}: {query}")

        # The SDK automatically tracks repetitive queries and kills on 3rd duplicate
        pulse.record_step(
            step_type="tool",
            title=f"Tool Call #{i+1}: sql_exec",
            content=f"Execution timed out. Retrying query attempt #{i+1}.",
            tool_call_str=query,
            input_tokens=2500,
            output_tokens=350,
        )

    return "Completed (should not reach here)"


# ── Example 3: Runaway Context Expansion (Cost Ceiling Guardrail) ─────────────

@pulse.monitor(
    agent_id="code-refactor-bot",
    model="claude-3-7-sonnet",
    circuit_breaker=CircuitBreaker(
        max_hops=10,
        max_cost_usd=0.40,  # Strict $0.40 cost limit
    )
)
def run_code_refactor_spike(prompt: str) -> str:
    """Simulates an agent accidentally ingesting massive 120k context, tripping budget guardrail."""
    print(f"\n💸 [Agent: code-refactor-bot] Ingesting large repository codebase...")
    time.sleep(0.15)

    # Hop 1: Normal hop
    pulse.record_step(
        step_type="thought",
        title="AST Indexing",
        content="Scanning repo dependency tree.",
        input_tokens=3000,
        output_tokens=400,
    )

    # Hop 2: Accidental ingestion of 90,000 token bundle ($3.00/1M = >$0.40 cost limit)
    time.sleep(0.2)
    print("  ↳ Ingesting 95,000 tokens of raw minified JS bundle (will exceed $0.40 budget)...")
    pulse.record_step(
        step_type="tool",
        title="Tool Call: read_file_bundle()",
        content="Ingested entire node_modules directory by mistake.",
        tool_call_str='read_file_bundle(path="./dist/bundle.js")',
        input_tokens=95000,
        output_tokens=15000,
    )

    return "Finished"


# ── Main Runner ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("=" * 68)
    print("  AgentPulse Ops — Live Telemetry & Circuit Breaker Demonstration")
    print("  Backend API:  http://localhost:8000")
    print("  Dashboard UI: http://localhost:3031")
    print("=" * 68)

    # Test 1
    print("\n── Test 1: Healthy Agent Run ───────────────────────────────────────")
    res1 = run_financial_research(
        prompt="Analyze NVDA Q3 2026 earnings call: gross margins and data center guidance."
    )
    print(f"  Result: {res1}")

    # Test 2
    print("\n── Test 2: Infinite Loop Interception ──────────────────────────────")
    try:
        run_sql_analyst_infinite_loop(
            prompt="Find churned customers in subscriptions table."
        )
    except CircuitBreakerException as e:
        print(f"\n🛡️  Circuit Breaker Aborted Run in <1ms!")
        print(f"   Reason: {e.reason}")
        print(f"   Hops at termination: {e.hops} | Prevented spend: ${e.cost_usd:.4f} | Tokens: {e.tokens:,}")

    # Test 3
    print("\n── Test 3: Budget Ceiling Interception ─────────────────────────────")
    try:
        run_code_refactor_spike(
            prompt="Refactor all handlers across codebase."
        )
    except CircuitBreakerException as e:
        print(f"\n🛡️  Circuit Breaker Cost Ceiling Triggered!")
        print(f"   Reason: {e.reason}")
        print(f"   Cost ceiling: ${e.cost_usd:.4f} (Budget limit: $0.40)")

    print("\n" + "=" * 68)
    print("  ✅ All 3 real telemetry traces logged to SQLite database!")
    print("  Refresh the dashboard at http://localhost:3031 to see them live.")
    print("=" * 68 + "\n")
