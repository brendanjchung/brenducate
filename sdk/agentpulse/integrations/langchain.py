"""
AgentPulse SDK — LangChain & LangGraph Integration Callback Handler

Usage:
    from agentpulse import AgentPulse, CircuitBreaker
    from agentpulse.integrations.langchain import AgentPulseCallbackHandler
    from langchain.agents import create_openai_tools_agent, AgentExecutor

    pulse = AgentPulse(api_key="ap_live_...")
    handler = AgentPulseCallbackHandler(
        pulse=pulse,
        agent_id="langchain-agent",
        circuit_breaker=CircuitBreaker(max_hops=6, max_cost_usd=0.50)
    )

    executor = AgentExecutor(agent=agent, tools=tools, callbacks=[handler])
    result = executor.invoke({"input": "Analyze customer support ticket"})
"""

import time
from typing import Any, Dict, List, Optional
from ..circuit_breaker import CircuitBreaker, CircuitBreakerException, ExecutionState, calculate_token_cost
from ..monitor import AgentPulse

try:
    from langchain_core.callbacks.base import BaseCallbackHandler
except ImportError:
    # Graceful fallback if langchain_core is not installed
    class BaseCallbackHandler:
        pass


class AgentPulseCallbackHandler(BaseCallbackHandler):
    """
    LangChain & LangGraph Callback Handler.
    Automatically captures LLM generations, tool calls, token usage, latency,
    and enforces AgentPulse Circuit Breakers on LangChain workflows.
    """

    def __init__(
        self,
        pulse: Optional[AgentPulse] = None,
        agent_id: str = "langchain-agent",
        circuit_breaker: Optional[CircuitBreaker] = None,
    ):
        if pulse is None:
            import os
            api_key = os.getenv("AGENTPULSE_API_KEY", "ap_live_demo_key_00001")
            base_url = os.getenv("AGENTPULSE_BASE_URL", "http://localhost:8000")
            pulse = AgentPulse(api_key=api_key, base_url=base_url)

        self.pulse = pulse
        self.agent_id = agent_id
        self.breaker = circuit_breaker or CircuitBreaker()
        self.state = ExecutionState()
        self.start_time = 0.0
        self.initial_prompt = ""
        self.model_name = "claude-3-7-sonnet"
        self._active_runs: Dict[str, float] = {}

    def on_chain_start(
        self, serialized: Dict[str, Any], inputs: Dict[str, Any], **kwargs: Any
    ) -> None:
        """Capture top-level agent execution start."""
        if not self.initial_prompt:
            self.start_time = time.monotonic()
            # Capture user goal from inputs
            for key in ("input", "query", "question", "prompt", "task"):
                if key in inputs:
                    self.initial_prompt = str(inputs[key])[:500]
                    break
            if not self.initial_prompt and inputs:
                self.initial_prompt = str(inputs)[:500]

            self.state.add_step(
                step_type="thought",
                title="LangChain Chain Started",
                content=f"Workflow initiated with objective: {self.initial_prompt[:120]}...",
            )

    def on_tool_start(
        self, serialized: Dict[str, Any], input_str: str, **kwargs: Any
    ) -> None:
        """Capture tool invocation and enforce circuit breaker limits."""
        tool_name = serialized.get("name", "tool") if isinstance(serialized, dict) else "tool"
        tool_call_str = f"{tool_name}({input_str[:120]})"
        
        # Enforce hop & loop limits in real time!
        self.state.record_hop(
            breaker=self.breaker,
            input_tokens=0,
            output_tokens=0,
            cost_usd=0.0,
            tool_call_str=tool_call_str,
            step_type="tool",
            title=f"Tool Call: {tool_name}",
            content=f"Arguments: {input_str[:250]}",
        )

    def on_tool_end(self, output: str, **kwargs: Any) -> None:
        """Record tool output."""
        if self.state.steps:
            last_step = self.state.steps[-1]
            if last_step.get("step_type") == "tool":
                last_step["content"] += f"\nOutput: {str(output)[:250]}"

    def on_llm_start(
        self, serialized: Dict[str, Any], prompts: List[str], **kwargs: Any
    ) -> None:
        """Capture LLM call start."""
        if serialized and "name" in serialized:
            self.model_name = serialized["name"]

    def on_llm_end(self, response: Any, **kwargs: Any) -> None:
        """Extract LLM usage statistics (tokens & cost)."""
        input_tokens = 0
        output_tokens = 0
        llm_output = getattr(response, "llm_output", None) or {}
        if isinstance(llm_output, dict):
            token_usage = llm_output.get("token_usage", {})
            if token_usage:
                input_tokens = token_usage.get("prompt_tokens", 0) or 0
                output_tokens = token_usage.get("completion_tokens", 0) or 0
            model_used = llm_output.get("model_name", self.model_name)
            self.model_name = model_used

        cost = calculate_token_cost(self.model_name, input_tokens, output_tokens)
        self.state.input_tokens += input_tokens
        self.state.output_tokens += output_tokens
        self.state.cost_usd += cost

        # Check cost ceiling guardrail
        if self.state.cost_usd > self.breaker.max_cost_usd:
            raise CircuitBreakerException(
                reason=f"Single-run cost ceiling exceeded (${self.state.cost_usd:.4f} > ${self.breaker.max_cost_usd:.2f})",
                hops=self.state.hops,
                cost_usd=self.state.cost_usd,
                tokens=self.state.total_tokens,
            )

    def on_chain_end(self, outputs: Dict[str, Any], **kwargs: Any) -> None:
        """Finalize trace and ship to AgentPulse edge ingestion API."""
        latency_ms = int((time.monotonic() - self.start_time) * 1000)
        output_str = str(outputs.get("output", outputs))[:300]
        
        self.state.add_step(
            step_type="final",
            title="LangChain Response Completed",
            content=output_str,
        )

        self.pulse._ship_trace(
            agent_id=self.agent_id,
            model=self.model_name,
            prompt=self.initial_prompt or "LangChain execution",
            state=self.state,
            status="COMPLETED",
            latency_ms=latency_ms,
            guardrail_reason=None,
            circuit_tripped=False,
        )

    def on_chain_error(self, error: BaseException, **kwargs: Any) -> None:
        """Capture chain error or circuit breaker interception."""
        latency_ms = int((time.monotonic() - self.start_time) * 1000)
        is_breaker = isinstance(error, CircuitBreakerException)
        status = "CIRCUIT_KILLED" if is_breaker else "ERROR"

        self.state.add_step(
            step_type="guardrail",
            title="Circuit Breaker Tripped" if is_breaker else "Execution Error",
            content=str(error),
        )

        self.pulse._ship_trace(
            agent_id=self.agent_id,
            model=self.model_name,
            prompt=self.initial_prompt or "LangChain execution",
            state=self.state,
            status=status,
            latency_ms=latency_ms,
            guardrail_reason=error.reason if is_breaker else str(error),
            circuit_tripped=is_breaker,
        )

