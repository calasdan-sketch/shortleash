"""Parse Claude Code `--output-format stream-json` (JSONL) events into observations.

Event shapes verified against the Claude Code docs (code.claude.com, Oct 2026:
"Run Claude Code programmatically" and the Agent SDK "Stream responses" page):
  {"type":"system","subtype":"init","session_id":..., "model":...}
  {"type":"assistant","message":{"id":"msg_...","content":[...blocks...],
     "usage":{"input_tokens":N,"output_tokens":N,
              "cache_creation_input_tokens":N,"cache_read_input_tokens":N}},
     "parent_tool_use_id": null|"toolu_..."}
  {"type":"result","subtype":"success","is_error":false,
     "total_cost_usd":X,"usage":{...},"num_turns":N,"session_id":...}

Gotchas handled:
- One API response is emitted as SEVERAL `assistant` events: "Claude Code
  emits an AssistantMessage as each non-empty content block completes, so a
  response with a text block and a tool call yields two AssistantMessage
  objects. Each one carries only its own content block, and both share the
  same message ID." Each copy carries the response's `usage`, so adding
  them up double-counts. The observation therefore carries `message_id`
  and the guard de-duplicates usage per message id (see Guard._add_usage).
- Subagent spawns are `tool_use` blocks of the subagent tool, named
  `Agent` in current Claude Code (the docs' "Agent tool") and `Task` in
  older versions: both names count. Each block carries its own `id`, which
  the guard uses so a re-emitted block is never counted twice. Subagents'
  own messages appear in the stream too (with `parent_tool_use_id` set);
  their usage is real spend and is counted like the main conversation's.
- `result.subtype` "success" can lie; `is_error` is authoritative. The guard
  treats a result with `is_error: true` as a failed run (exit 1 when the
  child itself exited 0) and says so in the receipt.
- Hook noise (system/hook_started, hook_response, api_retry) is ignored.
- Non-JSON lines (warnings, entrypoint chatter) are skipped, never fatal.
"""

from __future__ import annotations

import json

#: tool_use block names that mean "a subagent was spawned".
SPAWN_TOOL_NAMES = frozenset({"Agent", "Task"})


def _usage_of(message: dict) -> dict:
    u = (message or {}).get("usage") or {}
    return {
        "input": int(u.get("input_tokens") or 0),
        "output": int(u.get("output_tokens") or 0),
        "cache_write": int(u.get("cache_creation_input_tokens") or 0),
        "cache_read": int(u.get("cache_read_input_tokens") or 0),
    }


def _spawn_blocks(message: dict) -> list[str]:
    """Ids of subagent-spawning tool_use blocks (`Agent`/`Task`) in an
    assistant message. A block without an id gets a synthetic one per
    position so it still counts exactly once for this event."""
    ids: list[str] = []
    for i, block in enumerate((message or {}).get("content") or []):
        if isinstance(block, dict) and block.get("type") == "tool_use":
            if block.get("name") in SPAWN_TOOL_NAMES:
                ids.append(str(block.get("id") or f"<no-id>#{i}"))
    return ids


def _count_spawns(message: dict) -> int:
    """Count subagent spawns (`Agent`/`Task` tool_use blocks) in a message."""
    return len(_spawn_blocks(message))


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
        spawn_ids = _spawn_blocks(message)
        return {
            "kind": "usage",
            "message_id": str(message.get("id") or ""),
            "usage": _usage_of(message),
            "spawns": len(spawn_ids),
            "spawn_ids": spawn_ids,
        }

    if etype == "result":
        text = ev.get("result")
        return {
            "kind": "result",
            "cost_usd": ev.get("total_cost_usd"),
            "is_error": bool(ev.get("is_error")),
            "subtype": ev.get("subtype") or "",
            "num_turns": int(ev.get("num_turns") or 0),
            # The run's final totals, or None when the report has none.
            "usage": (_usage_of({"usage": ev["usage"]})
                      if isinstance(ev.get("usage"), dict) else None),
            "session_id": ev.get("session_id") or "",
            # The error message when is_error is set ("Not logged in", ...).
            # Bounded: never store a whole final answer in the receipt.
            "error_text": (str(text)[:200] if ev.get("is_error") and text
                           else ""),
        }

    return None
