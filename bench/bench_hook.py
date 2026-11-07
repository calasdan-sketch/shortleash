#!/usr/bin/env python3
"""Hook latency benchmark: how long `shortleash hook` takes per tool call as
the session grows.

Builds a SYNTHETIC Claude Code session in a temp directory (a main
transcript plus subagent transcripts, same line shapes the hook reads),
then runs the real `python -m shortleash hook` command the way Claude Code
does -- one process per tool call, the event on stdin -- and reports the
median latency of:

  cold   the first call on the session (nothing cached)
  warm   a call after one more message was appended to the transcript

    python3 bench/bench_hook.py              # default ~40 MB session
    python3 bench/bench_hook.py --mb 150 --agents 100

Nothing leaves the temp directory; no network, no real transcripts.
"""

import argparse
import json
import os
import statistics
import subprocess
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def assistant(i: int, model: str = "claude-sonnet-5-5") -> str:
    return json.dumps({"type": "assistant", "message": {
        "id": f"msg_{i}", "model": model,
        "content": [{"type": "text", "text": "x" * 600},
                    {"type": "tool_use", "id": f"toolu_{i}", "name": "Bash",
                     "input": {"command": f"echo step {i}"}}],
        "usage": {"input_tokens": 10, "output_tokens": 200,
                  "cache_creation_input_tokens": 500,
                  "cache_read_input_tokens": 20000}}})


def user(i: int) -> str:
    return json.dumps({"type": "user", "message": {"content": [
        {"type": "tool_result", "tool_use_id": f"toolu_{i}",
         "content": "y" * 600}]}})


def write_transcript(path: str, start: int, count: int) -> None:
    with open(path, "w", encoding="utf-8") as f:
        for i in range(start, start + count):
            f.write(assistant(i) + "\n" + user(i) + "\n")


def append_one(path: str, i: int) -> None:
    with open(path, "a", encoding="utf-8") as f:
        f.write(assistant(i) + "\n" + user(i) + "\n")


def call_hook(env: dict, transcript: str, n: int) -> float:
    event = {"session_id": "bench", "transcript_path": transcript,
             "hook_event_name": "PreToolUse", "tool_name": "Bash",
             "tool_use_id": f"toolu_new_{n}",
             "tool_input": {"command": f"echo new {n}"}}
    t0 = time.perf_counter()
    r = subprocess.run([sys.executable, "-m", "shortleash", "hook",
                        "--budget", "1000000", "--max-repeats", "4",
                        "--max-spawns", "50"],
                       input=json.dumps(event), text=True, env=env,
                       cwd=ROOT, capture_output=True)
    dt = time.perf_counter() - t0
    if r.returncode != 0:
        raise SystemExit(f"hook failed: {r.stderr}")
    return dt


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mb", type=float, default=40.0,
                    help="approximate size of the whole session (default 40)")
    ap.add_argument("--agents", type=int, default=40,
                    help="number of subagent transcripts (default 40)")
    ap.add_argument("--calls", type=int, default=7,
                    help="warm calls to time (default 7)")
    args = ap.parse_args()
    per_pair = len(assistant(0)) + len(user(0)) + 2
    pairs = max(1, int(args.mb * 1024 * 1024 / per_pair))
    per_file = max(1, pairs // (args.agents + 1))
    with tempfile.TemporaryDirectory() as tmp:
        env = dict(os.environ, SHORTLEASH_HOME=os.path.join(tmp, "state"))
        proj = os.path.join(tmp, "proj")
        os.makedirs(os.path.join(proj, "sess", "subagents"))
        main_t = os.path.join(proj, "sess.jsonl")
        write_transcript(main_t, 0, per_file)
        for a in range(args.agents):
            write_transcript(os.path.join(proj, "sess", "subagents",
                                          f"agent-{a}.jsonl"),
                             (a + 1) * per_file, per_file)
        size = sum(os.path.getsize(os.path.join(d, f))
                   for d, _, fs in os.walk(proj) for f in fs)
        cold = call_hook(env, main_t, 0)
        warm = []
        for n in range(1, args.calls + 1):
            append_one(main_t, 10_000_000 + n)
            warm.append(call_hook(env, main_t, n))
        print(f"session: {size / 1024 / 1024:.1f} MB in {args.agents + 1} "
              f"transcripts")
        print(f"cold call:        {cold * 1000:8.0f} ms")
        print(f"warm call median: {statistics.median(warm) * 1000:8.0f} ms "
              f"(n={len(warm)}, max {max(warm) * 1000:.0f} ms)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
