"""Per-run receipt: what happened, what it cost, why it stopped."""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone

from . import __version__

# Any argument whose NAME matches one of these (case-insensitive) gets its
# VALUE replaced with *** in receipts. Matches --api-key, api_key=, -token,
# --db-password value, etc.
_SECRET_NAME_RE = re.compile(r"key|token|secret|password|passwd",
                             re.IGNORECASE)
_FLAG_EQ_RE = re.compile(r"^(--?)([A-Za-z0-9_][A-Za-z0-9_-]*)=(.*)$")
_BARE_EQ_RE = re.compile(r"^([A-Za-z0-9_][A-Za-z0-9_-]*)=(.*)$")
_FLAG_RE = re.compile(r"^(--?)([A-Za-z0-9_][A-Za-z0-9_-]*)$")


def _is_secret_name(name: str) -> bool:
    return _SECRET_NAME_RE.search(name) is not None


def redact_argv(argv: list[str]) -> list[str]:
    """Return argv with secret values replaced by ***.

    Never mutate the original. Handles three shapes:
      --api-key sk-123   -> ["--api-key", "***"]   (flag + value pair)
      --api-key=sk-123   -> ["--api-key=***"]
      api_key=sk-123     -> ["api_key=***"]
    Matching is substring on the argument name (e.g. "monkey" would also
    match — over-redaction is the safe direction).
    """
    out: list[str] = []
    redact_next = False
    for a in argv:
        if redact_next:
            out.append("***")
            redact_next = False
            continue
        m = _FLAG_EQ_RE.match(a)
        if m:
            prefix, name, value = m.groups()
            out.append(f"{prefix}{name}="
                       + ("***" if _is_secret_name(name) else value))
            continue
        m = _BARE_EQ_RE.match(a)
        if m:
            name, value = m.groups()
            out.append(f"{name}="
                       + ("***" if _is_secret_name(name) else value))
            continue
        m = _FLAG_RE.match(a)
        if m and _is_secret_name(m.group(2)):
            out.append(a)
            redact_next = True
            continue
        out.append(a)
    return out


def build_receipt(
    *,
    command: list[str],
    started_at: float,
    ended_at: float,
    breached: bool,
    breach_reason: str | None,
    cost_usd: float,
    cost_estimated: bool,
    usage: dict,
    num_turns: int,
    spawns: int,
    exit_code: int | None,
    model: str,
    session_id: str,
    agent: str = "",
    profile: str = "",
    baseline: dict | None = None,
) -> dict:
    return {
        "tool": "shortleash",
        "version": __version__,
        # Never persist raw argv: redact anything that looks like a secret.
        "command": redact_argv(command),
        "agent": agent,
        "profile": profile,
        "baseline": baseline,
        "started_at": datetime.fromtimestamp(started_at, tz=timezone.utc).isoformat(),
        "ended_at": datetime.fromtimestamp(ended_at, tz=timezone.utc).isoformat(),
        "duration_s": round(ended_at - started_at, 2),
        "breached": breached,
        "breach_reason": breach_reason,
        "cost_usd": round(cost_usd, 6),
        "cost_estimated": cost_estimated,
        "tokens": {
            "input": usage.get("input", 0),
            "output": usage.get("output", 0),
            "cache_write": usage.get("cache_write", 0),
            "cache_read": usage.get("cache_read", 0),
        },
        "num_turns": num_turns,
        "subagent_spawns": spawns,
        "exit_code": exit_code,
        "model": model,
        "session_id": session_id,
    }


def format_human(receipt: dict) -> str:
    t = receipt["tokens"]
    total_tokens = t["input"] + t["output"] + t["cache_write"] + t["cache_read"]
    lines = [
        "",
        "── shortleash receipt ──────────────────────",
        f"  cost:      ${receipt['cost_usd']:.4f}"
        + (" (estimated)" if receipt["cost_estimated"] else " (reported)"),
        f"  tokens:    {total_tokens:,} "
        f"(in={t['input']:,} out={t['output']:,} "
        f"cache_r={t['cache_read']:,} cache_w={t['cache_write']:,})",
        f"  turns:     {receipt['num_turns']}   spawns: {receipt['subagent_spawns']}",
        f"  duration:  {receipt['duration_s']}s   exit: {receipt['exit_code']}",
    ]
    if receipt["breached"]:
        lines.append(f"  STOPPED:   {receipt['breach_reason']}")
    else:
        lines.append("  finished within guardrails ✓")
    baseline = receipt.get("baseline")
    if baseline:
        lines.append(f"  baseline:  typical ${baseline['median_cost']:.4f} / "
                     f"{baseline['median_tokens']:,} tokens "
                     f"({baseline['runs']} past runs)")
    lines.append("──────────────────────────────────────────────────")
    return "\n".join(lines)


def write_receipt(receipt: dict, path: str) -> None:
    with open(path, "w") as f:
        json.dump(receipt, f, indent=2)
