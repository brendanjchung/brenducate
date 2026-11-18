"""
Brenducate SDK — Model Pricing, Prompt Cache Math, & Same-Family Step-Down Map

Prices are USD per 1,000,000 tokens.
Handles the critical billing difference between providers:
  - OpenAI: `prompt_tokens` INCLUDES `cached_tokens` -> uncached = prompt_tokens - cached_tokens.
  - Anthropic: `input_tokens` is UNCACHED only; `cache_read_input_tokens` and
    `cache_creation_input_tokens` are separate additive buckets.
"""

from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple


# USD per 1,000,000 tokens
MODEL_PRICING: Dict[str, Dict[str, float]] = {
    # Anthropic Claude models
    "claude-opus-4-5": {
        "input": 15.00,
        "cached_input": 1.50,
        "cache_write": 18.75,
        "output": 75.00,
    },
    "claude-opus-4": {
        "input": 15.00,
        "cached_input": 1.50,
        "cache_write": 18.75,
        "output": 75.00,
    },
    "claude-sonnet-4-5": {
        "input": 3.00,
        "cached_input": 0.30,
        "cache_write": 3.75,
        "output": 15.00,
    },
    "claude-sonnet-4": {
        "input": 3.00,
        "cached_input": 0.30,
        "cache_write": 3.75,
        "output": 15.00,
    },
    "claude-3-7-sonnet": {
        "input": 3.00,
        "cached_input": 0.30,
        "cache_write": 3.75,
        "output": 15.00,
    },
    "claude-3-5-sonnet": {
        "input": 3.00,
        "cached_input": 0.30,
        "cache_write": 3.75,
        "output": 15.00,
    },
    "claude-haiku-4-5": {
        "input": 1.00,
        "cached_input": 0.10,
        "cache_write": 1.25,
        "output": 5.00,
    },
    "claude-3-5-haiku": {
        "input": 0.80,
        "cached_input": 0.08,
        "cache_write": 1.00,
        "output": 4.00,
    },
    "claude-3-haiku": {
        "input": 0.25,
        "cached_input": 0.03,
        "cache_write": 0.30,
        "output": 1.25,
    },
    # OpenAI models
    "gpt-5-nano": {
        "input": 0.05,
        "cached_input": 0.005,
        "cache_write": 0.05,
        "output": 0.40,
    },
    "gpt-5-mini": {
        "input": 0.25,
        "cached_input": 0.025,
        "cache_write": 0.25,
        "output": 2.00,
    },
    "gpt-5.4": {
        "input": 2.50,
        "cached_input": 0.25,
        "cache_write": 2.50,
        "output": 15.00,
    },
    "gpt-5": {
        "input": 1.25,
        "cached_input": 0.125,
        "cache_write": 1.25,
        "output": 10.00,
    },
    "gpt-4.1": {
        "input": 2.00,
        "cached_input": 0.50,
        "cache_write": 2.00,
        "output": 8.00,
    },
    "gpt-4.1-mini": {
        "input": 0.40,
        "cached_input": 0.10,
        "cache_write": 0.40,
        "output": 1.60,
    },
    "gpt-4.1-nano": {
        "input": 0.10,
        "cached_input": 0.025,
        "cache_write": 0.10,
        "output": 0.40,
    },
    "gpt-4o": {
        "input": 2.50,
        "cached_input": 1.25,
        "cache_write": 2.50,
        "output": 10.00,
    },
    "gpt-4o-mini": {
        "input": 0.15,
        "cached_input": 0.075,
        "cache_write": 0.15,
        "output": 0.60,
    },
    "o1": {
        "input": 15.00,
        "cached_input": 7.50,
        "cache_write": 15.00,
        "output": 60.00,
    },
    "o3": {
        "input": 2.00,
        "cached_input": 0.50,
        "cache_write": 2.00,
        "output": 8.00,
    },
    "o3-mini": {
        "input": 1.10,
        "cached_input": 0.55,
        "cache_write": 1.10,
        "output": 4.40,
    },
    "o4-mini": {
        "input": 1.10,
        "cached_input": 0.275,
        "cache_write": 1.10,
        "output": 4.40,
    },
    "default": {
        "input": 2.00,
        "cached_input": 1.00,
        "cache_write": 2.00,
        "output": 8.00,
    },
}

# Pre-sorted longest keys first so "gpt-4o-mini" always matches before "gpt-4o"
_PRICING_KEYS_SORTED = sorted(
    (k for k in MODEL_PRICING.keys() if k != "default"),
    key=len,
    reverse=True,
)

# Default same-family Smart Degradation mapping (already-cheap models map to None)
DEFAULT_DEGRADATION_MAP: Dict[str, Optional[str]] = {
    "claude-opus-4-5": "claude-sonnet-4-5-20250929",
    "claude-opus-4": "claude-sonnet-4-20250514",
    "claude-sonnet-4-5": "claude-haiku-4-5-20251001",
    "claude-sonnet-4": "claude-3-5-haiku-latest",
    "claude-3-7-sonnet": "claude-3-5-haiku-latest",
    "claude-3-5-sonnet": "claude-3-5-haiku-latest",
    "claude-haiku-4-5": None,
    "claude-3-5-haiku": None,
    "claude-3-haiku": None,
    "gpt-5.4": "gpt-5-mini",
    "gpt-5": "gpt-5-mini",
    "gpt-5-mini": "gpt-5-nano",
    "gpt-5-nano": None,
    "gpt-4.1": "gpt-4.1-mini",
    "gpt-4.1-mini": None,
    "gpt-4.1-nano": None,
    "gpt-4o": "gpt-4o-mini",
    "gpt-4o-mini": None,
    "o1": "o3-mini",
    "o3": "o3-mini",
    "o3-mini": None,
    "o4-mini": None,
}


@dataclass
class TokenUsage:
    input_tokens: int = 0          # Uncached input tokens
    cached_input_tokens: int = 0   # Cache read tokens
    cache_creation_tokens: int = 0 # Anthropic cache write tokens
    output_tokens: int = 0         # Total completion tokens (includes reasoning tokens)
    reasoning_tokens: int = 0      # Subset of output_tokens used for hidden reasoning

    @property
    def total_tokens(self) -> int:
        return (
            self.input_tokens
            + self.cached_input_tokens
            + self.cache_creation_tokens
            + self.output_tokens
        )


def _get_attr_or_key(obj: Any, name: str, default: Any = None) -> Any:
    if obj is None:
        return default
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


def get_model_pricing(model_name: str) -> Dict[str, float]:
    """Lookup pricing table matching exact key or longest model prefix."""
    clean = (model_name or "").lower().strip()
    if clean in MODEL_PRICING:
        return MODEL_PRICING[clean]
    for key in _PRICING_KEYS_SORTED:
        if key in clean:
            return MODEL_PRICING[key]
    return MODEL_PRICING["default"]


def resolve_step_down_model(
    model_name: str,
    custom_map: Optional[Dict[str, str]] = None,
) -> Optional[str]:
    """
    Resolve a cheaper same-family fallback model for Smart Degradation.
    Returns None if the model is already the lowest tier or has no mapping.
    """
    clean = (model_name or "").lower().strip()
    if not clean:
        return None

    if custom_map:
        if model_name in custom_map:
            return custom_map[model_name]
        if clean in custom_map:
            return custom_map[clean]
        for key in sorted(custom_map.keys(), key=len, reverse=True):
            if key.lower() in clean:
                target = custom_map[key]
                return target if target and target.lower() != clean else None

    if clean in DEFAULT_DEGRADATION_MAP:
        return DEFAULT_DEGRADATION_MAP[clean]
    for key in sorted(DEFAULT_DEGRADATION_MAP.keys(), key=len, reverse=True):
        if key in clean:
            return DEFAULT_DEGRADATION_MAP[key]
    return None


def extract_openai_usage(usage_obj: Any) -> TokenUsage:
    """
    Extract normalized TokenUsage from an OpenAI Chat Completions OR Responses API `.usage`.
    Note: OpenAI `prompt_tokens` / `input_tokens` already includes `cached_tokens`.
    """
    if not usage_obj:
        return TokenUsage()

    prompt_val = _get_attr_or_key(usage_obj, "prompt_tokens")
    if prompt_val is None:
        prompt_val = _get_attr_or_key(usage_obj, "input_tokens", 0)
    total_prompt = int(prompt_val or 0)

    comp_val = _get_attr_or_key(usage_obj, "completion_tokens")
    if comp_val is None:
        comp_val = _get_attr_or_key(usage_obj, "output_tokens", 0)
    total_completion = int(comp_val or 0)

    prompt_details = (
        _get_attr_or_key(usage_obj, "prompt_tokens_details")
        or _get_attr_or_key(usage_obj, "input_tokens_details")
    )
    cached = int(_get_attr_or_key(prompt_details, "cached_tokens", 0) or 0)

    comp_details = (
        _get_attr_or_key(usage_obj, "completion_tokens_details")
        or _get_attr_or_key(usage_obj, "output_tokens_details")
    )
    reasoning = int(_get_attr_or_key(comp_details, "reasoning_tokens", 0) or 0)

    uncached_input = max(0, total_prompt - cached)
    return TokenUsage(
        input_tokens=uncached_input,
        cached_input_tokens=cached,
        cache_creation_tokens=0,
        output_tokens=total_completion,
        reasoning_tokens=reasoning,
    )


def extract_anthropic_usage(usage_obj: Any) -> TokenUsage:
    """
    Extract normalized TokenUsage from an Anthropic `message.usage` object or dict.
    Note: Anthropic `input_tokens` only counts uncached input tokens.
    """
    if not usage_obj:
        return TokenUsage()

    uncached_input = int(_get_attr_or_key(usage_obj, "input_tokens", 0) or 0)
    cached_read = int(_get_attr_or_key(usage_obj, "cache_read_input_tokens", 0) or 0)
    cache_create = int(_get_attr_or_key(usage_obj, "cache_creation_input_tokens", 0) or 0)
    output = int(_get_attr_or_key(usage_obj, "output_tokens", 0) or 0)

    return TokenUsage(
        input_tokens=uncached_input,
        cached_input_tokens=cached_read,
        cache_creation_tokens=cache_create,
        output_tokens=output,
        reasoning_tokens=0,
    )


def calculate_cost(model_name: str, usage: TokenUsage) -> float:
    """Compute exact USD cost from normalized TokenUsage and model pricing."""
    p = get_model_pricing(model_name)
    cost = (
        (usage.input_tokens / 1_000_000.0) * p["input"]
        + (usage.cached_input_tokens / 1_000_000.0) * p["cached_input"]
        + (usage.cache_creation_tokens / 1_000_000.0) * p["cache_write"]
        + (usage.output_tokens / 1_000_000.0) * p["output"]
    )
    return round(cost, 6)


def calculate_token_cost(
    model_name: str,
    input_tokens: int,
    output_tokens: int,
    cached_input_tokens: int = 0,
    cache_creation_tokens: int = 0,
) -> float:
    """Convenience helper to compute USD cost directly from integer token counts."""
    return calculate_cost(
        model_name,
        TokenUsage(
            input_tokens=input_tokens,
            cached_input_tokens=cached_input_tokens,
            cache_creation_tokens=cache_creation_tokens,
            output_tokens=output_tokens,
        ),
    )


def calculate_degradation_savings(
    original_model: str,
    executed_model: str,
    usage: TokenUsage,
) -> float:
    """Estimate USD saved by executing on the stepped-down same-family model."""
    if not original_model or not executed_model or original_model == executed_model:
        return 0.0
    baseline = calculate_cost(original_model, usage)
    actual = calculate_cost(executed_model, usage)
    return round(max(0.0, baseline - actual), 6)
