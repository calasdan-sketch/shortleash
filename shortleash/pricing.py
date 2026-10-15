"""Cost estimation from token usage.

Mid-run kill decisions need a live cost estimate, but the authoritative
`total_cost_usd` only arrives in the final `result` event. This module
estimates from per-message usage using a pricing table.

DEFAULT_PRICING values are FALLBACK ESTIMATES — override with --pricing
(pointing at a JSON file) for exact numbers. The final receipt always
prefers the agent-reported total_cost_usd when present.
"""

from __future__ import annotations

import json

# USD per 1M tokens. (input, output). Cache read ~10% of input, cache
# write ~1.25x input — standard Anthropic cache pricing shape.
DEFAULT_PRICING = {
    "opus": (15.00, 75.00),
    "sonnet": (3.00, 15.00),
    "haiku": (0.80, 4.00),
    "default": (3.00, 15.00),
}

CACHE_READ_FACTOR = 0.10
CACHE_WRITE_FACTOR = 1.25


def load_pricing(path: str | None) -> dict:
    if not path:
        return dict(DEFAULT_PRICING)
    with open(path) as f:
        data = json.load(f)
    pricing = dict(DEFAULT_PRICING)
    for k, v in data.items():
        pricing[str(k).lower()] = (float(v[0]), float(v[1]))
    return pricing


def _rate_for(model: str, pricing: dict) -> tuple[float, float]:
    m = (model or "").lower()
    for key in pricing:
        if key != "default" and key in m:
            return pricing[key]
    return pricing["default"]


def estimate_cost(model: str, usage: dict, pricing: dict | None = None) -> float:
    """Estimate USD cost for a usage dict {input, output, cache_write, cache_read}."""
    pricing = pricing or DEFAULT_PRICING
    in_rate, out_rate = _rate_for(model, pricing)
    u = usage
    tokens = (
        u.get("input", 0) * in_rate
        + u.get("output", 0) * out_rate
        + u.get("cache_read", 0) * in_rate * CACHE_READ_FACTOR
        + u.get("cache_write", 0) * in_rate * CACHE_WRITE_FACTOR
    )
    return tokens / 1_000_000
