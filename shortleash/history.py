"""Run history + baselines: learn what a normal run costs.

Every receipt is appended to ~/.shortleash/history.jsonl. Before a run,
the guard loads the baseline for the agent binary (median cost/tokens over
past runs) and trips only when the live run exceeds
baseline * anomaly_multiplier. A naturally large task establishes its own
baseline over time; only true runaways trip.

Grouping key: the agent binary name (argv[0] basename), overridable with
--profile. Minimum history (default 5 runs) before anomaly mode arms —
until then only static caps apply.
"""

from __future__ import annotations

import json
import os
import statistics

DEFAULT_HISTORY_PATH = os.path.expanduser("~/.shortleash/history.jsonl")
MIN_HISTORY = 5


def record(receipt: dict, path: str | None = None) -> None:
    path = path or DEFAULT_HISTORY_PATH
    os.makedirs(os.path.dirname(path), exist_ok=True)
    entry = {
        "agent": receipt.get("agent", ""),
        "profile": receipt.get("profile", ""),
        "cost_usd": receipt.get("cost_usd", 0.0),
        "tokens": sum(receipt.get("tokens", {}).values()),
        "breached": receipt.get("breached", False),
        "ended_at": receipt.get("ended_at", ""),
    }
    with open(path, "a") as f:
        f.write(json.dumps(entry) + "\n")


def _key(entry: dict) -> str:
    return entry.get("profile") or entry.get("agent") or ""


def baseline(agent: str, profile: str = "",
             path: str | None = None,
             min_runs: int = MIN_HISTORY) -> dict | None:
    """Return {median_cost, median_tokens, runs} or None if not enough
    history. Breached runs are excluded — they were, by definition,
    not normal."""
    path = path or DEFAULT_HISTORY_PATH
    if not os.path.isfile(path):
        return None
    want = profile or agent
    costs: list[float] = []
    tokens: list[int] = []
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    e = json.loads(line)
                except ValueError:
                    continue
                if e.get("breached"):
                    continue
                if _key(e) != want:
                    continue
                costs.append(float(e.get("cost_usd", 0) or 0))
                tokens.append(int(e.get("tokens", 0) or 0))
    except OSError:
        return None
    if len(costs) < min_runs:
        return None
    return {
        "median_cost": statistics.median(costs),
        "median_tokens": statistics.median(tokens),
        "runs": len(costs),
    }
