"""Cost estimation from token usage.

Mid-run kill decisions need a live cost estimate, but the authoritative
`total_cost_usd` only arrives in the final `result` event. This module
estimates from per-message usage using a pricing table.

DEFAULT_PRICING values are FALLBACK ESTIMATES, hard-coded on
PRICING_AS_OF — they are NOT fetched from anywhere and are not verified
against the providers' current price lists. Verify them against the
provider pricing page before trusting a --budget to the cent, and
override them with --pricing FILE or the SHORTLEASH_PRICING env var
(both: a JSON file, see load_table). The final receipt always prefers
the agent-reported total_cost_usd when present.

Pricing JSON format (USD per 1M tokens, [input, output]):

    {"as_of": "2026-10-16",
     "sonnet": [3.0, 15.0], "opus": [15.0, 75.0], "haiku": [0.8, 4.0]}

"as_of" is optional metadata and is echoed in the receipt; every other
key is a case-insensitive substring matched against the model name.
"""

from __future__ import annotations

import dataclasses
import json
import os

#: Date the hard-coded table below was written. Nothing updates it
#: automatically: when you change a number, change this too.
PRICING_AS_OF = "2026-10-16"

#: Environment variable naming a pricing JSON file (used when --pricing
#: is not given).
PRICING_ENV = "SHORTLEASH_PRICING"

# USD per 1M tokens. (input, output). Cache read ~10% of input, cache
# write ~1.25x input — standard Anthropic cache pricing shape.
# Fallback estimates as of PRICING_AS_OF — verify against provider pricing.
DEFAULT_PRICING = {
    "opus": (15.00, 75.00),
    "sonnet": (3.00, 15.00),
    "haiku": (0.80, 4.00),
    "default": (3.00, 15.00),
}

CACHE_READ_FACTOR = 0.10
CACHE_WRITE_FACTOR = 1.25

#: Keys in a pricing JSON file that are metadata, not model rates.
_META_KEYS = {"as_of"}


@dataclasses.dataclass
class PricingTable:
    """Rates plus where they came from, so receipts can say so."""
    rates: dict                      # model substring -> (in, out) per 1M
    as_of: str = PRICING_AS_OF       # ISO date the numbers were written
    source: str = "built-in fallback table"

    def describe(self) -> str:
        return (f"{self.source} as of {self.as_of} "
                f"— verify against provider pricing")


def _parse_rates(data: dict, pricing: dict, where: str) -> str | None:
    """Merge model -> [in, out] pairs from a JSON object into pricing.

    Returns the file's as_of string (or None). Raises ValueError with
    the offending key for anything that is not a two-number list — a
    metadata string such as "as_of" must never be read as a rate pair.
    """
    if not isinstance(data, dict):
        raise ValueError(f"{where}: top level must be a JSON object")
    as_of = None
    for k, v in data.items():
        key = str(k).lower()
        if key in _META_KEYS:
            if key == "as_of":
                as_of = str(v)
            continue
        if (not isinstance(v, (list, tuple)) or len(v) != 2
                or isinstance(v[0], (bool, str)) or isinstance(v[1], (bool, str))):
            raise ValueError(
                f"{where}: {k!r} must be [input_per_1M, output_per_1M], got {v!r}")
        pricing[key] = (float(v[0]), float(v[1]))
    return as_of


def load_pricing(path: str | None) -> dict:
    """Return the rates dict (defaults overlaid with the JSON file at path)."""
    if not path:
        return dict(DEFAULT_PRICING)
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    pricing = dict(DEFAULT_PRICING)
    _parse_rates(data, pricing, path)
    return pricing


def load_table(path: str | None = None,
               environ: dict | None = None) -> PricingTable:
    """Resolve the pricing table: --pricing path, else $SHORTLEASH_PRICING,
    else the built-in fallback. Raises OSError/ValueError on a bad file."""
    env = os.environ if environ is None else environ
    source_path = path or env.get(PRICING_ENV) or None
    if not source_path:
        return PricingTable(rates=dict(DEFAULT_PRICING))
    with open(source_path, encoding="utf-8") as f:
        data = json.load(f)
    rates = dict(DEFAULT_PRICING)
    as_of = _parse_rates(data, rates, source_path)
    label = source_path if path else f"{source_path} (${PRICING_ENV})"
    return PricingTable(rates=rates, as_of=as_of or "unknown (no as_of in file)",
                        source=label)


def _rates_of(pricing) -> dict:
    if isinstance(pricing, PricingTable):
        return pricing.rates
    return pricing or DEFAULT_PRICING


def _rate_for(model: str, pricing: dict) -> tuple[float, float]:
    m = (model or "").lower()
    for key in pricing:
        if key != "default" and key in m:
            return pricing[key]
    return pricing["default"]


def estimate_cost(model: str, usage: dict, pricing=None) -> float:
    """Estimate USD cost for a usage dict {input, output, cache_write, cache_read}.

    ``pricing`` may be a rates dict or a PricingTable.
    """
    rates = _rates_of(pricing)
    in_rate, out_rate = _rate_for(model, rates)
    u = usage
    tokens = (
        u.get("input", 0) * in_rate
        + u.get("output", 0) * out_rate
        + u.get("cache_read", 0) * in_rate * CACHE_READ_FACTOR
        + u.get("cache_write", 0) * in_rate * CACHE_WRITE_FACTOR
    )
    return tokens / 1_000_000
