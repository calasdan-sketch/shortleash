#!/usr/bin/env python3
"""Process-supervision benchmark: how many processes of a misbehaving agent
are still running after the leash is done with it?

The fake agent (sim/orphans.py) leaves three sleepers behind: one in its
process group, one that called setsid() and one double-forked daemon.
Scenarios, each run with the sentinel/ancestry sweep OFF (the behaviour
before 0.3.0) and ON:

  breach    a --budget breach kills the tree
  kill -9   the leash itself is SIGKILLed mid-run (nothing can be handled)
  SIGTERM   the leash is terminated

Prints survivors per scenario (lower is better; 0 is the goal). Linux/macOS
only (it needs fork and process groups); survivors are counted by a unique
tag on the sleepers' command lines, and killed at the end.

    python3 bench/bench_orphans.py
"""

import os
import signal
import subprocess
import sys
import tempfile
import time
import uuid

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
AGENT = os.path.join(ROOT, "sim", "orphans.py")


def survivors(tag: str) -> list[int]:
    out = subprocess.run(["pgrep", "-f", f"time.sleep\\(600\\).*{tag}|{tag}"],
                         capture_output=True, text=True).stdout.split()
    return [int(p) for p in out if int(p) != os.getpid()]


def scenario(name: str, sweep: bool) -> int:
    tag = "slb" + uuid.uuid4().hex[:12]
    with tempfile.TemporaryDirectory() as tmp:
        env = dict(os.environ, SHORTLEASH_HOME=tmp)
        if not sweep:
            env["SHORTLEASH_NO_ESCAPE_SWEEP"] = "1"
        args = [sys.executable, "-m", "shortleash", "run", "--quiet"]
        if name == "breach":
            args += ["--budget", "0.5"]
        else:
            args += ["--budget", "1000"]
        args += ["--", sys.executable, AGENT, tag, "--spend"]
        p = subprocess.Popen(args, cwd=ROOT, env=env,
                             stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL)
        if name == "breach":
            p.wait(timeout=60)
        else:
            time.sleep(2.5)
            p.send_signal(signal.SIGKILL if name == "kill -9"
                          else signal.SIGTERM)
            p.wait(timeout=60)
        time.sleep(1.5)
        left = survivors(tag)
        for pid in left:
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass
        return len(left)


def main() -> int:
    if os.name != "posix" or not os.path.isdir("/proc/self"):
        print("this benchmark needs Linux (/proc)")
        return 0
    print(f"{'scenario':10} {'sweep off':>10} {'sweep on':>10}   "
          f"(survivors of 3 sleepers; lower is better)")
    for name in ("breach", "kill -9", "SIGTERM"):
        off = scenario(name, False)
        on = scenario(name, True)
        print(f"{name:10} {off:>10} {on:>10}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
