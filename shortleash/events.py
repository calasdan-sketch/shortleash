"""Parse Claude Code `--output-format stream-json` (JSONL) events into observations.

Event shapes verified against community docs (Oct 2026):
  {"type":"system","subtype":"init","session_id":..., "model":...}
  {"type":"assistant","message":{"content":[...blocks...],
     "usage":{"input_tokens":N,"output_tokens":N,
              "cache_creation_input_tokens":N,"cache_read_input_tokens":N}}}
  {"type":"result","subtype":"success","is_error":false,
     "total_cost_usd":X,"usage":{...},"num_turns":N,"session_id":...}

Gotchas handled:
- result.subtype "success" can lie; terminal success is gated on is_error==False.
- Hook noise (system/hook_started, hook_response, api_retry) is ignored.
- Non-JSON lines (warnings, entrypoint chatter) are skipped, never fatal.
"""

from __future__ import annotations

import json


def _usage_of(message: dict) -> dict:
    u = (message or {}).get("usage") or {}
    return {
        "input": int(u.get("input_tokens") or 0),
        "output": int(u.get("output_tokens") or 0),
        "cache_write": int(u.get("cache_creation_input_tokens") or 0),
        "cache_read": int(u.get("cache_read_input_tokens") or 0),
    }


def _count_spawns(message: dict) -> int:
    """Count subagent spawns: Task tool_use blocks in an assistant message."""
    n = 0
    for block in (message or {}).get("content") or []:
        if isinstance(block, dict) and block.get("type") == "tool_use":
            if block.get("name") == "Task":
                n += 1
    return n


def parse_line(line: str) -> dict | None:
    """Parse one stdout line. Returns an observation dict or None to ignore."""
    line = line.strip()
    if not line or not line.startswith("{"):
        return None
    try:
        ev = json.loads(line)
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(ev, dict):
        return None

    etype = ev.get("type")

    if etype == "system" and ev.get("subtype") == "init":
        return {
            "kind": "init",
            "model": ev.get("model") or "",
            "session_id": ev.get("session_id") or "",
        }

    if etype == "assistant":
        message = ev.get("message") or {}
        return {
            "kind": "usage",
            "usage": _usage_of(message),
            "spawns": _count_spawns(message),
        }

    if etype == "result":
        return {
            "kind": "result",
            "cost_usd": ev.get("total_cost_usd"),
            "is_error": bool(ev.get("is_error")),
            "subtype": ev.get("subtype") or "",
            "num_turns": int(ev.get("num_turns") or 0),
            "usage": _usage_of({"usage": ev.get("usage")}),
            "session_id": ev.get("session_id") or "",
        }

    return None
