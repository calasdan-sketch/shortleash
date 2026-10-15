#!/usr/bin/env python3
"""Fake agent that emits Claude Code stream-json telemetry.

Used to test shortleash without spending real API dollars.
Simulates a runaway: growing token usage per turn and subagent spawns.

Usage:
    python sim/runaway.py --turns 30 --tokens-per-turn 20000 --spawns 12 --delay 0.2
    python sim/runaway.py --well-behaved   # stays tiny, exits cleanly
"""

import argparse
import json
import sys
import time


def emit(obj: dict) -> None:
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--turns", type=int, default=20)
    p.add_argument("--tokens-per-turn", type=int, default=15000)
    p.add_argument("--spawns", type=int, default=8,
                   help="Total Task tool_use blocks spread across turns.")
    p.add_argument("--cost", type=float, default=4.27,
                   help="total_cost_usd reported in the final result event.")
    p.add_argument("--delay", type=float, default=0.15,
                   help="Seconds between turns (simulates real-time streaming).")
    p.add_argument("--well-behaved", action="store_true",
                   help="Emit a tiny 2-turn run instead.")
    args = p.parse_args()

    if args.well_behaved:
        turns, tpt, spawns, cost = 2, 800, 0, 0.012
    else:
        turns, tpt, spawns, cost = args.turns, args.tokens_per_turn, args.spawns, args.cost

    emit({"type": "system", "subtype": "init",
          "session_id": "sim-session-001", "model": "claude-sonnet-4-5",
          "tools": ["Read", "Edit", "Bash", "Task"]})

    # Distribute spawns across turns (at least 1 per turn until exhausted).
    spawns_left = spawns
    for i in range(turns):
        content = [{"type": "text", "text": f"Working on step {i + 1}..."}]
        n_spawn = 0
        if spawns_left > 0:
            n_spawn = 1 if i < turns - 1 else spawns_left
            spawns_left -= n_spawn
            for s in range(n_spawn):
                content.append({"type": "tool_use", "id": f"task-{i}-{s}",
                                "name": "Task",
                                "input": {"description": "delegate subtask"}})
        # Usage grows per turn (runaway shape).
        scale = 1 + i * 0.15
        emit({"type": "assistant",
              "message": {"content": content,
                          "usage": {"input_tokens": int(tpt * scale),
                                    "output_tokens": int(tpt * scale * 0.3),
                                    "cache_creation_input_tokens": int(tpt * scale * 0.1),
                                    "cache_read_input_tokens": int(tpt * scale * 2)}}})
        time.sleep(args.delay)

    emit({"type": "result", "subtype": "success", "is_error": False,
          "result": "done", "total_cost_usd": cost,
          "num_turns": turns, "session_id": "sim-session-001",
          "usage": {"input_tokens": 1000, "output_tokens": 100}})
    return 0


if __name__ == "__main__":
    sys.exit(main())
