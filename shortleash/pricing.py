"""Cost estimation from token usage.

Mid-run kill decisions need a live cost estimate, but the authoritative
`total_cost_usd` only arrives in the final `result` event. This module
estimates from per-message usage using a pricing table.

DEFAULT_PRICING values are FALLBACK ESTIMATES, hard-coded on
PRICING_AS_OF — they are NOT fetched from anywhere and are not verified
against the providers' current price lists. Verify them against the
provider pricing page before trusting a --budget to the cent, and
override them with --pricing FILE or the SHORTLEASH_PRICING env var
(both: a JSON file, see load_table; UTF-8, or UTF-16/UTF-32 with a
byte-order mark, as Windows tools write them). The final receipt always
prefers the agent-reported total_cost_usd when present.

Pricing JSON format (USD per 1M tokens, [input, output]):

    {"as_of": "2026-10-16",
     "sonnet": [3.0, 15.0], "opus": [15.0, 75.0], "haiku": [0.8, 4.0]}

"as_of" is optional metadata and is echoed in the receipt; every other
key is a case-insensitive substring matched against the model name. The
entries of your file are tried first and the built-in table after them; in
each, the LONGEST matching key wins ("opus-4-5" beats "opus"). A model that
matches nothing is priced with the "default" entry, and the receipt says so
(`price_assumed`). Rates must be finite numbers >= 0.
"""

from __future__ import annotations

import dataclasses
import datetime
import json
import math
import os

from . import textfile

#: Date the hard-coded table below was written. Nothing updates it
#: automatically: when you change a number, change this too.
PRICING_AS_OF = "2026-10-24"

#: Environment variable naming a pricing JSON file (used when --pricing
#: is not given).
PRICING_ENV = "SHORTLEASH_PRICING"

# USD per 1M tokens. (input, output). Cache read ~10% of input, cache
# write ~1.25x input - the classic Anthropic cache pricing shape (a few
# newest models charge less for cache reads; the estimate then runs high,
# the safe side for a budget cap).
# Source: https://platform.claude.com/docs/en/about-claude/pricing
# (formerly docs.claude.com), read on PRICING_AS_OF. A bare family key
# ("opus") is the CURRENT generation; older generations that cost more or
# less have their own, longer keys, which win (longest match).
DEFAULT_PRICING = {
    "opus": (5.00, 25.00),         # Opus 4.5 / 4.6 / 4.7 / 4.8 / 5
    "opus-5-5": (4.00, 20.00),
    "opus-4-1": (15.00, 75.00),
    "opus-4-2025": (15.00, 75.00),  # claude-opus-4-<date> (Opus 4)
    "opus-4-0": (15.00, 75.00),
    "opus-4": (15.00, 75.00),      # bare "claude-opus-4"
    "opus-4-5": (5.00, 25.00),     # beat "opus-4" above
    "opus-4-6": (5.00, 25.00),
    "opus-4-7": (5.00, 25.00),
    "opus-4-8": (5.00, 25.00),
    "opus-5": (5.00, 25.00),
    "3-opus": (15.00, 75.00),      # claude-3-opus
    "sonnet": (3.00, 15.00),       # Sonnet 4 / 4.5 / 4.6 (and 3.x)
    "sonnet-5": (2.00, 10.00),     # Sonnet 5 / 5.5
    "haiku": (1.00, 5.00),         # Haiku 4.5
    "haiku-3-5": (0.80, 4.00),
    "3-5-haiku": (0.80, 4.00),
    "haiku-5": (0.50, 2.50),       # Haiku 5.5: the >100k-prompt rate (the
                                   # <=100k rate is 0.10 / 0.50); high = safe
    "fable": (10.00, 50.00),
    "mythos": (10.00, 50.00),
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
    # The entries that came from the user's file (a subset of ``rates``):
    # they are tried before the built-in ones.
    overrides: dict = dataclasses.field(default_factory=dict)

    def describe(self) -> str:
        return (f"{self.source} as of {self.as_of} "
                f"— verify against provider pricing")


#: A price table older than this many days gets a warning at start.
STALE_AFTER_DAYS = 90


def staleness(as_of: str, today=None) -> str | None:
    """A warning line when the table is more than STALE_AFTER_DAYS old, or
    its date cannot be read; None when it is fresh. ``today`` is a
    datetime.date (tests pass one; no clock is read then)."""
    try:
        written = datetime.date.fromisoformat(str(as_of)[:10])
    except ValueError:
        return (f"the price table has no usable date ({as_of!r}): check the "
                f"prices before trusting --budget")
    today = today or datetime.date.today()
    age = (today - written).days
    if age > STALE_AFTER_DAYS:
        return (f"the price table is {age} days old (written {written}): "
                f"prices may have changed; check them, or give --pricing "
                f"FILE with a current as_of")
    return None


def _parse_rates(data: dict, pricing: dict, where: str,
                 overrides: dict | None = None) -> str | None:
    """Merge model -> [in, out] pairs from a JSON object into pricing.

    Returns the file's as_of string (or None). Raises ValueError with
    the offending key for anything that is not a two-number list of finite,
    non-negative numbers — a metadata string such as "as_of" must never be
    read as a rate pair, and a NaN or negative rate would silently disable
    or invert the budget cap. Every pair also goes into ``overrides``.
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
        if not key.strip():
            raise ValueError(f"{where}: a model key must not be empty "
                             f"(it would match every model)")
        try:
            rate = (float(v[0]), float(v[1]))
        except (TypeError, ValueError, OverflowError):
            rate = (math.nan, math.nan)
        if not all(math.isfinite(x) and x >= 0 for x in rate):
            raise ValueError(
                f"{where}: {k!r} rates must be finite numbers >= 0, got {v!r}")
        pricing[key] = rate
        if overrides is not None:
            overrides[key] = rate
    return as_of


def load_table(path: str | None = None,
               environ: dict | None = None) -> PricingTable:
    """Resolve the pricing table: --pricing path, else $SHORTLEASH_PRICING,
    else the built-in fallback. Raises OSError/ValueError on a bad file."""
    env = os.environ if environ is None else environ
    source_path = path or env.get(PRICING_ENV) or None
    if not source_path:
        return PricingTable(rates=dict(DEFAULT_PRICING))
    with open(source_path, "rb") as f:
        raw = f.read()
    try:
        text = textfile.decode_text(raw)
    except ValueError as e:
        raise ValueError(f"the file {e}") from None
    data = json.loads(text)
    rates = dict(DEFAULT_PRICING)
    overrides: dict = {}
    as_of = _parse_rates(data, rates, source_path, overrides)
    label = source_path if path else f"{source_path} (${PRICING_ENV})"
    return PricingTable(rates=rates, as_of=as_of or "unknown (no as_of in file)",
                        source=label, overrides=overrides)


def _rates_of(pricing) -> dict:
    if isinstance(pricing, PricingTable):
        return pricing.rates
    return pricing or DEFAULT_PRICING


def _longest_key(model: str, table: dict) -> str | None:
    best = None
    for key in table:
        if key != "default" and key in model and (
                best is None or len(key) > len(best)):
            best = key
    return best


def match(model: str, pricing=None) -> tuple[str, tuple[float, float]]:
    """Which table entry prices ``model``: (key, (input, output) per 1M).

    The user's own entries are tried first, then the whole table; in each
    the longest key contained in the model name wins. ``"default"`` as the
    key means nothing matched and the fallback rate is an assumption.
    ``pricing`` may be a rates dict or a PricingTable."""
    m = (model or "").lower()
    rates = _rates_of(pricing)
    overrides = pricing.overrides if isinstance(pricing, PricingTable) else {}
    for table in (overrides, rates):
        key = _longest_key(m, table)
        if key is not None:
            return key, rates[key]
    return "default", rates.get("default", DEFAULT_PRICING["default"])


def _rate_for(model: str, pricing: dict) -> tuple[float, float]:
    return match(model, pricing)[1]


def estimate_cost(model: str, usage: dict, pricing=None) -> float:
    """Estimate USD cost for a usage dict {input, output, cache_write, cache_read}.

    ``pricing`` may be a rates dict or a PricingTable.
    """
    in_rate, out_rate = match(model, pricing)[1]
    u = usage
    try:
        tokens = (
            u.get("input", 0) * in_rate
            + u.get("output", 0) * out_rate
            + u.get("cache_read", 0) * in_rate * CACHE_READ_FACTOR
            + u.get("cache_write", 0) * in_rate * CACHE_WRITE_FACTOR
        )
    except OverflowError:
        # A count too large for a float: the cost is "more than anything",
        # which compares above every cap -- never an exception.
        return math.inf
    return tokens / 1_000_000
