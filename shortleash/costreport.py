"""`shortleash report`: what the guarded runs cost over the last N days, from
the history file (history.jsonl). Only what the history holds is summed:
agent, profile, cost, tokens, breached flag and end time. A run that was not
recorded (no telemetry) is not in it."""

from __future__ import annotations

import datetime

from . import history, paths


def _when(text) -> datetime.datetime | None:
    try:
        d = datetime.datetime.fromisoformat(str(text))
    except ValueError:
        return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=datetime.timezone.utc)
    return d


def summarize(path: str | None = None, days: int = 7,
              now: datetime.datetime | None = None) -> dict:
    """{"days", "runs", "cost_usd", "tokens", "breached", "by_key": {...},
    "by_day": {...}, "skipped"}. Lines that are not well-formed entries, or
    have no readable end time, are counted in "skipped"."""
    path = path or history.default_history_path()
    now = now or datetime.datetime.now(datetime.timezone.utc)
    since = now - datetime.timedelta(days=days)
    out = {"days": days, "runs": 0, "cost_usd": 0.0, "tokens": 0,
           "breached": 0, "by_key": {}, "by_day": {}, "skipped": 0}
    try:
        f = open(path, encoding="utf-8", errors="surrogateescape")
    except OSError:
        return out
    with f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            parsed = history._parse_entry(line)
            if parsed is None:
                out["skipped"] += 1
                continue
            key, cost, tokens, breached = parsed
            import json
            try:
                at = _when(json.loads(line).get("ended_at"))
            except ValueError:
                at = None
            if at is None:
                out["skipped"] += 1
                continue
            if at < since or at > now + datetime.timedelta(days=1):
                continue
            out["runs"] += 1
            out["cost_usd"] += cost
            out["tokens"] += tokens
            out["breached"] += 1 if breached else 0
            k = out["by_key"].setdefault(key or "?", {"runs": 0, "cost_usd": 0.0})
            k["runs"] += 1
            k["cost_usd"] += cost
            day = at.astimezone(datetime.timezone.utc).date().isoformat()
            d = out["by_day"].setdefault(day, {"runs": 0, "cost_usd": 0.0})
            d["runs"] += 1
            d["cost_usd"] += cost
    return out


def to_text(r: dict) -> list[str]:
    lines = [f"shortleash: last {r['days']} day(s), UTC dates",
             f"  {r['runs']} guarded run(s), ${r['cost_usd']:.2f}, "
             f"{r['tokens']:,} tokens, {r['breached']} stopped by a limit"]
    if r["by_key"]:
        lines.append("  by agent/profile:")
        for k, v in sorted(r["by_key"].items(), key=lambda kv: -kv[1]["cost_usd"]):
            lines.append(f"    {k:24} {v['runs']:>4} run(s)  ${v['cost_usd']:.2f}")
    if r["by_day"]:
        lines.append("  by day:")
        for d in sorted(r["by_day"]):
            v = r["by_day"][d]
            lines.append(f"    {d}  {v['runs']:>4} run(s)  ${v['cost_usd']:.2f}")
    if r["skipped"]:
        lines.append(f"  ({r['skipped']} history line(s) skipped: damaged or "
                     f"without an end time)")
    lines.append("  Costs are the estimates and agent-reported totals the "
                 "receipts held; not an invoice.")
    return lines


DEFAULT_PROFILE = """\
# .shortleash.toml written by `shortleash init`: sensible starting limits.
budget = 5.00        # USD per `shortleash run`
max_spawns = 10      # subagent starts (also the hook's per-session limit)
timeout = 1800       # seconds (30 minutes)
"""
