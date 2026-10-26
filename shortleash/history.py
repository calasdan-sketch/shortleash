"""Run history + baselines: learn what a normal run costs.

Every receipt with telemetry is appended to ~/.shortleash/history.jsonl
(no command text or prompt is stored -- only agent, profile, cost, token
total, breached flag and end time). With --anomaly, the guard loads the
baseline for the agent binary (median cost/tokens over past runs) and
trips when the live run exceeds baseline * anomaly_multiplier.

Anomaly mode is OPT-IN (--anomaly). Breached runs are excluded from the
baseline, which has a consequence worth knowing: a legitimately larger
task that gets stopped as an anomaly never enters the baseline, so it
would be stopped again on every attempt. The STOPPED line says how to get
out of that: re-run without --anomaly so the run is recorded, or give the
task its own --profile baseline.

Grouping key: the agent binary name (argv[0] basename), overridable with
--profile. Minimum history (default 5 runs) before anomaly mode arms --
until then only static caps apply.

A corrupt history line (bad JSON, a non-numeric cost, a byte that is not
UTF-8) is skipped with a warning; it must never stop the agent from starting.
"""

from __future__ import annotations

import json
import math
import os
import statistics

from . import paths

MIN_HISTORY = 5


def default_history_path() -> str:
    """~/.shortleash/history.jsonl, or under $SHORTLEASH_HOME (see paths)."""
    return paths.history_path()


#: Figures beyond these are not a real run's: a baseline built from them
#: (a median of absurd numbers) would stop every later run from starting.
_MAX_COST = 1e12
_MAX_TOKENS = 10 ** 18


def _sane(cost, tokens) -> bool:
    return (isinstance(cost, (int, float)) and math.isfinite(cost)
            and abs(cost) <= _MAX_COST and abs(tokens) <= _MAX_TOKENS)


def record(receipt: dict, path: str | None = None) -> None:
    """Append one run to the history. A run whose figures are not usable
    numbers (no cost, infinite, absurd) is not recorded: it says nothing
    about what a normal run costs."""
    path = path or default_history_path()
    cost = receipt.get("cost_usd", 0.0)
    tokens = sum(receipt.get("tokens", {}).values())
    if not _sane(cost, tokens):
        return
    paths.private_dir(os.path.dirname(path))
    entry = {
        "agent": receipt.get("agent", ""),
        "profile": receipt.get("profile", ""),
        "cost_usd": cost,
        "tokens": tokens,
        "breached": receipt.get("breached", False),
        "ended_at": receipt.get("ended_at", ""),
    }
    paths.append_private(
        path, (json.dumps(entry, allow_nan=False) + "\n").encode("utf-8"))


def _key(entry: dict) -> str:
    return entry.get("profile") or entry.get("agent") or ""


def _parse_entry(line: str) -> tuple[str, float, int, bool] | None:
    """(key, cost, tokens, breached) from one history line, or None when
    the line is not a well-formed entry (bad JSON, non-numeric fields, a byte
    that is not UTF-8: _runs reads the file with errors="surrogateescape", so
    such a byte arrives here as a lone surrogate)."""
    try:
        line.encode("utf-8")
    except UnicodeEncodeError:
        return None  # a torn write or a hand edit left a damaged line
    try:
        e = json.loads(line)
    except ValueError:
        return None
    if not isinstance(e, dict):
        return None
    try:
        cost = float(e.get("cost_usd", 0) or 0)
        tokens = int(e.get("tokens", 0) or 0)
    except (TypeError, ValueError, OverflowError):
        return None
    if not _sane(cost, tokens):
        return None  # Infinity / NaN / absurd: corrupt, not a data point
    return _key(e), cost, tokens, bool(e.get("breached"))


def _runs(agent: str, profile: str, path: str | None,
          warnings: list[str] | None) -> tuple[list[float], list[int]]:
    """(costs, tokens) of the non-breached past runs for agent/profile.
    Corrupt lines are skipped; a note is appended to ``warnings`` (when
    given) saying how many. A missing or unreadable file is no history."""
    path = path or default_history_path()
    want = profile or agent
    costs: list[float] = []
    tokens: list[int] = []
    skipped = 0
    try:
        # surrogateescape: a byte that is not UTF-8 must not stop the read
        # (UnicodeDecodeError would escape every handler here); _parse_entry
        # counts such a line as corrupt.
        with open(path, encoding="utf-8", errors="surrogateescape") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                parsed = _parse_entry(line)
                if parsed is None:
                    skipped += 1
                    continue
                key, cost, toks, breached = parsed
                if breached or key != want:
                    continue
                costs.append(cost)
                tokens.append(toks)
    except OSError:
        return [], []
    if skipped and warnings is not None:
        warnings.append(
            f"history: skipped {skipped} corrupt line(s) in {path}")
    return costs, tokens


def count_runs(agent: str, profile: str = "",
               path: str | None = None) -> int:
    """How many non-breached past runs the baseline for agent/profile
    would be built from (so a receipt can say "not armed (2 of 5 runs)")."""
    return len(_runs(agent, profile, path, None)[0])


def baseline(agent: str, profile: str = "",
             path: str | None = None,
             min_runs: int = MIN_HISTORY,
             warnings: list[str] | None = None) -> dict | None:
    """Return {median_cost, median_tokens, runs} or None if not enough
    history. Breached runs are excluded -- they were, by definition,
    not normal. Corrupt lines are skipped; a note is appended to
    ``warnings`` (when given) saying how many."""
    costs, tokens = _runs(agent, profile, path, warnings)
    if not costs or len(costs) < min_runs:
        # (a baseline is a median: with no past run at all there is none,
        # whatever min_runs says)
        return None
    return {
        "median_cost": statistics.median(costs),
        "median_tokens": statistics.median(tokens),
        "runs": len(costs),
    }
