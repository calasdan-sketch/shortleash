#!/usr/bin/env python3
"""Loop-detection benchmark: which stuck agents are caught, and which healthy
ones are wrongly stopped.

The corpus is SYNTHETIC and written by the maintainers (no external dataset,
no real transcripts): tool-call traces of the shapes real agents produce.
STUCK traces should be stopped; HEALTHY traces must never be. Each trace is
replayed call by call exactly as the hook sees it, under three settings:

  exact          --max-repeats 4                        (0.2.x behaviour)
  fuzzy          --max-repeats 4 --loop-fuzzy
  fuzzy+errors   --max-repeats 4 --loop-fuzzy --max-errors 6

Prints recall (stuck traces stopped) and false positives (healthy traces
stopped) for each. Add traces to CORPUS to test your own worry.

    python3 bench/bench_loops.py
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from shortleash import loops  # noqa: E402

REPEATS = 4
MAX_ERRORS = 6


def bash(cmd, out, err=False):
    return ("Bash", {"command": cmd}, out, err)


def stuck_exact():
    return [bash("pytest tests/test_x.py -x", "FAILED test_x.py::test_a - assert 1 == 2", True)
            for _ in range(8)]


def stuck_timestamp_in_input():
    return [bash(f"curl -s localhost:8080/health?ts={1730000000 + i * 5}",
                 '{"status":"down","at":"2026-11-07T10:00:%02dZ"}' % i)
            for i in range(8)]


def stuck_health_poll_sleep():
    return [bash(f"sleep {5 + 0} && curl -s localhost:8080/health",
                 f'{{"status":"starting","checked":"2026-11-07T10:{i:02d}:07Z"}}')
            for i in range(8)]


def stuck_cycle_with_durations():
    out = []
    for i in range(6):
        out.append(("Read", {"file_path": "/app/src/main.py"}, "def main(): ...", False))
        out.append(bash("make build", f"error: undefined symbol foo\nFinished in {3 + i * 0.1:.1f}s", True))
    return out


def stuck_whitespace_variants():
    cmds = ["grep -rn TODO src", "grep  -rn TODO src", "grep -rn  TODO src",
            "grep -rn TODO  src", "grep   -rn TODO src", "grep -rn TODO src "]
    return [bash(c, "src/a.py:3: TODO fix") for c in cmds] * 2


def stuck_uuid_in_result():
    return [bash("docker run --rm app:latest", f"container 3f2a9c1e-7b4d-4e0a-9d51-{i:012x} exited with code 1", True)
            for i in range(8)]


def stuck_varied_failures():
    pkgs = ["numpy", "scipy", "pandas", "pyarrow", "polars", "duckdb", "numba", "torch"]
    return [bash(f"pip install {p}", f"ERROR: Could not find a version that satisfies {p} (no network)", True)
            for p in pkgs]


def stuck_varied_errors_mixed_tools():
    t = []
    for i in range(8):
        t.append(("Edit", {"file_path": f"/app/f{i}.py", "old_string": f"x{i}"},
                  f"String to replace not found in file (attempt {i})", True))
    return t


def healthy_edit_test():
    t = []
    for fails in (5, 4, 4, 3, 2, 1, 0):
        t.append(("Edit", {"file_path": "/app/a.py", "old_string": f"v{fails}", "new_string": f"v{fails - 1}"},
                  "ok", False))
        t.append(bash("pytest -q", f"{fails} failed, {20 - fails} passed in 1.2s", fails > 0))
    return t


def healthy_paginated_read():
    return [("Read", {"file_path": "/app/big.log", "offset": i * 200, "limit": 200},
             f"line {i * 200} ... content block {i}", False) for i in range(10)]


def healthy_polling_tools():
    return [("BashOutput", {"bash_id": "b1"}, "still running", False) for _ in range(12)]


def healthy_same_cmd_between_edits():
    t = []
    for i in range(6):
        t.append(bash("git status --short", f" M src/f{i}.py"))
        t.append(("Edit", {"file_path": f"/app/f{i}.py", "old_string": "a", "new_string": f"b{i}"}, "ok", False))
    return t


def healthy_tdd_errors_interleaved():
    t = []
    for i in range(10):
        t.append(bash(f"pytest tests/test_{i}.py", f"{i + 1} failed", True))
        t.append(bash(f"pytest tests/test_{i}.py", "ok", False))
    return t


def healthy_numbers_in_output_change():
    return [bash("python bench.py", f"throughput {1000 + i * 37} ops/s over 12 runs") for i in range(8)]


def healthy_polling_progress():
    return [bash("curl -s localhost:8080/progress", f'{{"done":{i * 12},"of":100}}') for i in range(8)]


def healthy_seed_sweep():
    # A sweep over seeds whose output is the same "ok" each time: fuzzy
    # mode (numbers ignored) takes it for a loop. Known cost of fuzzy mode.
    return [bash(f"python check.py --seed {i}", "ok: invariants hold") for i in range(8)]


CORPUS = [
    ("stuck", "same failing test, identical output", stuck_exact),
    ("stuck", "retry with a timestamp in the command", stuck_timestamp_in_input),
    ("stuck", "health poll, timestamp in the result", stuck_health_poll_sleep),
    ("stuck", "read+build cycle, durations differ", stuck_cycle_with_durations),
    ("stuck", "same grep, white space varies", stuck_whitespace_variants),
    ("stuck", "same docker run, a uuid in the result", stuck_uuid_in_result),
    ("stuck", "eight different installs, all fail", stuck_varied_failures),
    ("stuck", "eight different edits, all fail", stuck_varied_errors_mixed_tools),
    ("healthy", "edit-test cycle, failures decreasing", healthy_edit_test),
    ("healthy", "paginated read of one file", healthy_paginated_read),
    ("healthy", "polling a background job", healthy_polling_tools),
    ("healthy", "git status between edits", healthy_same_cmd_between_edits),
    ("healthy", "failing test then passing, repeated", healthy_tdd_errors_interleaved),
    ("healthy", "benchmark numbers differ each run", healthy_numbers_in_output_change),
    ("healthy", "progress poll, counter advances", healthy_polling_progress),
    ("healthy", "seed sweep, same output each time", healthy_seed_sweep),
]


def stopped(trace, fuzzy, max_errors):
    calls = []
    streak = 0
    for name, inp, out, err in trace:
        calls.append((loops.fingerprint(name, inp, fuzzy),
                      loops.result_digest(out, err, fuzzy)))
        streak = streak + 1 if err else 0
        if loops.find_loop(calls, REPEATS):
            return True
        if max_errors is not None and streak >= max_errors:
            return True
    return False


def main() -> int:
    settings = [("exact", False, None), ("fuzzy", True, None),
                ("fuzzy+errors", True, MAX_ERRORS)]
    stuck = [c for c in CORPUS if c[0] == "stuck"]
    healthy = [c for c in CORPUS if c[0] == "healthy"]
    print(f"{len(stuck)} stuck traces, {len(healthy)} healthy traces "
          f"(synthetic corpus, see the module docstring)\n")
    print(f"{'setting':14}{'stuck caught':>14}{'healthy stopped':>18}")
    for label, fuzzy, me in settings:
        caught = sum(stopped(t(), fuzzy, me) for _, _, t in stuck)
        fp = [d for _, d, t in healthy if stopped(t(), fuzzy, me)]
        print(f"{label:14}{caught:>10}/{len(stuck)}{len(fp):>14}/{len(healthy)}")
        for d in fp:
            print(f"    FALSE POSITIVE: {d}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
