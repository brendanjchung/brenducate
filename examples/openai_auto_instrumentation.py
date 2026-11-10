"""
AgentPulse Ops — OpenAI Auto-Instrumentation Example

Demonstrates how wrapping an OpenAI client with 1 line of code automatically:
  1. Tracks tokens (input/output) & computes dollar cost
  2. Measures latency (ms)
  3. Records step-by-step reasoning graphs
  4. Triggers sub-millisecond circuit breakers if repetitive calls or budgets are breached

Usage:
  python3 examples/openai_auto_instrumentation.py
"""

import sys
import os
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../sdk"))

from agentpulse import AgentPulse, CircuitBreaker, CircuitBreakerException, wrap_openai

# ── Mock OpenAI Client for Testing Without Live API Keys ──────────────────────

class MockChoice:
    def __init__(self, content):
        class Message:
            def __init__(self, c):
                self.content = c
        self.message = Message(content)

class MockUsage:
    def __init__(self, prompt_tokens, completion_tokens):
        self.prompt_tokens = prompt_tokens
        self.completion_tokens = completion_tokens

class MockCompletionResponse:
    def __init__(self, content, prompt_tokens=1800, completion_tokens=420):
        self.choices = [MockChoice(content)]
        self.usage = MockUsage(prompt_tokens, completion_tokens)

class MockCompletions:
    def create(self, model="gpt-4o", messages=None, **kwargs):
        time.sleep(0.15)  # Simulate API latency
        user_prompt = messages[-1]["content"] if messages else "Hello"
        return MockCompletionResponse(
            content=f"Summary of '{user_prompt[:40]}...': Processed successfully with verified references.",
            prompt_tokens=2200,
            completion_tokens=480
        )

class MockChat:
    def __init__(self):
        self.completions = MockCompletions()

class MockOpenAIClient:
    def __init__(self):
        self.chat = MockChat()


# ── Demonstration ─────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("=" * 68)
    print("  AgentPulse Ops — OpenAI 1-Line Auto-Instrumentation")
    print("=" * 68)

    pulse = AgentPulse(
        api_key="ap_live_demo_key_00001",
        base_url="http://localhost:8000",
        environment="production",
        verbose=True
    )

    # 1-Line auto-instrumentation wrapper!
    raw_client = MockOpenAIClient()
    client = wrap_openai(
        raw_client,
        pulse=pulse,
        agent_id="openai-market-analyst",
        circuit_breaker=CircuitBreaker(max_hops=5, max_cost_usd=0.20)
    )

    print("\nCalling client.chat.completions.create(...) with wrapped client:")
    response = client.chat.completions.create(
        model="gpt-4o",
        messages=[
            {"role": "system", "content": "You are a senior equity analyst."},
            {"role": "user", "content": "Analyze semiconductor supply chain constraints in Q3."}
        ]
    )

    print(f"\nResponse received: {response.choices[0].message.content}")
    print(f"Usage tracked: {response.usage.prompt_tokens} prompt, {response.usage.completion_tokens} completion")
    print("\n✅ Telemetry trace shipped automatically to AgentPulse dashboard!")

