"""
Brenducate — 1-Line Sync & Async OpenAI Auto-Instrumentation Example

Shows:
  - Per-tenant scoping via `with sentinel.tenant("acme_corp"):`
  - Automatic `stream_options={"include_usage": True}` injection
  - Same-family Smart Degradation when soft budget cap is crossed
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../sdk"))

from brenducate import Brenducate, CircuitBreaker, wrap_openai


class _Msg:
    def __init__(self, content):
        self.content = content


class _Choice:
    def __init__(self, content):
        self.message = _Msg(content)


class _UsageDetails:
    def __init__(self, cached_tokens=0, reasoning_tokens=0):
        self.cached_tokens = cached_tokens
        self.reasoning_tokens = reasoning_tokens


class _Usage:
    def __init__(self, prompt_tokens, completion_tokens, cached=0):
        self.prompt_tokens = prompt_tokens
        self.completion_tokens = completion_tokens
        self.prompt_tokens_details = _UsageDetails(cached_tokens=cached)
        self.completion_tokens_details = _UsageDetails()


class _Resp:
    def __init__(self, content, prompt_tokens=2400, completion_tokens=500, cached=1200):
        self.choices = [_Choice(content)]
        self.usage = _Usage(prompt_tokens, completion_tokens, cached=cached)


class _MockCompletions:
    def create(self, model="gpt-4o", messages=None, **kwargs):
        print(f"  [Provider received] model={model} | stream={kwargs.get('stream', False)}")
        return _Resp(f"Executed cleanly on {model}.")


class _MockChat:
    def __init__(self):
        self.completions = _MockCompletions()


class MockOpenAI:
    def __init__(self):
        self.chat = _MockChat()


if __name__ == "__main__":
    sentinel = Brenducate(
        api_key="br_live_demo_key_00001",
        base_url="http://localhost:8000",
        verbose=True,
        enable_background_sync=False,
    )

    client = wrap_openai(
        MockOpenAI(),
        pulse=sentinel,
        agent_id="support-agent",
        circuit_breaker=CircuitBreaker(degrade_after_cost_usd=0.05, max_cost_usd=1.00),
    )

    with sentinel.tenant("acme_corp"):
        r = client.chat.completions.create(
            model="gpt-4o",
            messages=[{"role": "user", "content": "Summarize ticket #481"}],
        )
        print("Output:", r.choices[0].message.content)

    sentinel.flush(timeout=2.0)
