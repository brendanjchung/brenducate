# AgentPulse Python SDK (`agentpulse-sdk`)

Production observability, prompt drift sentinel, and sub-millisecond circuit breakers for autonomous LLM agents (LangChain, AutoGen, CrewAI, LlamaIndex, or raw OpenAI/Anthropic).

## 🚀 Quickstart

### 1. Install
```bash
pip install agentpulse-sdk
# or install locally from source:
pip install -e sdk/
```

### 2. Wrap your Agent Function

```python
from agentpulse import AgentPulse, CircuitBreaker

# Initialize sentinel
pulse = AgentPulse(
    api_key="ap_live_your_api_key_here",
    base_url="http://localhost:8000",   # Use https://api.agentpulse.io in production
    environment="production"
)

# Protect your agent with a circuit breaker
@pulse.monitor(
    agent_id="customer-support-copilot",
    model="claude-3-7-sonnet",
    circuit_breaker=CircuitBreaker(
        max_hops=6,                     # Abort if agent loops > 6 hops
        max_cost_usd=0.50,              # Kill if run exceeds $0.50
        kill_on_infinite_loop=True,     # Kill repetitive duplicate tool calls
        max_duplicate_calls=2
    )
)
def run_my_agent(user_query: str):
    # Record reasoning steps or tool calls inside your loop
    pulse.record_step(
        step_type="tool",
        title="CRM Search",
        content="Searching customer ticket history",
        tool_call_str='crm.search(query="invoice 9812")',
        input_tokens=1500,
        output_tokens=300
    )
    
    # If the agent attempts a runaway recursive loop, CircuitBreakerException
    # is raised in <1ms, killing the run and logging the incident to your dashboard!
    return "Resolved query."
```

### 3. Context Manager Syntax

You can also use AgentPulse as an explicit context manager:

```python
with pulse.trace("sql-analyst", model="gpt-4o") as t:
    t.record_step(
        step_type="tool",
        title="Execute SQL",
        tool_call_str="SELECT * FROM users",
        input_tokens=800,
        output_tokens=200
    )
```

## 🛡️ Circuit Breakers & Guardrails

| Guardrail | Parameter | Default | Behavior |
| :--- | :--- | :--- | :--- |
| **Max Hops** | `max_hops` | `8` | Aborts run if total reasoning steps / tool calls exceeds limit. |
| **Cost Ceiling** | `max_cost_usd` | `$1.00` | Calculates token spend against live model pricing; aborts if exceeded. |
| **Infinite Recursion** | `kill_on_infinite_loop` | `True` | Intercepts repetitive duplicate queries with zero parameter change. |
| **Duplicate Threshold** | `max_duplicate_calls` | `2` | Number of allowable consecutive duplicate calls before termination. |

## 📦 Features
- **Zero latency impact**: Telemetry payloads are delivered asynchronously using non-blocking HTTP workers.
- **In-process guardrail checks**: All circuit-breaker decisions execute in memory in `<1ms` without waiting for network responses.
- **Auto cost calculation**: Built-in pricing matrix for Claude 3.7/3.5, GPT-4o, Gemini 2.5, DeepSeek R1, and Llama 3.3.
- **Framework agnostic**: Works natively with LangChain, AutoGen, CrewAI, LiteLLM, or plain Python functions.
