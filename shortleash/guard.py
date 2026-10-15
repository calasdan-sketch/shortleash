"""The guard: spawn the agent, tail its telemetry, kill it on breach.

Design:
- Child runs in its own process group (start_new_session=True) so a
  breach kill takes the whole tree, never our own group.
- A monitor thread reads the child's stdout line-by-line, parses
  stream-json events, passes every line through to our stdout
  (so the user still watches the agent work), and checks caps after
  each observation.
- On breach: SIGTERM to the group, grace period, then SIGKILL.
- A watchdog thread enforces --timeout even when the child goes silent.
- Exit code 124 on guard-triggered kill (mirrors timeout(1));
  otherwise the child's exit code.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import threading
import time

from . import events
from . import history as history_mod
from . import pricing
from . import receipt as receipt_mod

BREACH_EXIT_CODE = 124


class Guard:
    def __init__(
        self,
        *,
        budget: float | None = None,
        max_spawns: int | None = None,
        max_tokens: int | None = None,
        timeout: float | None = None,
        grace_period: float = 5.0,
        pricing_table: dict | None = None,
        receipt_path: str | None = None,
        quiet: bool = False,
        anomaly: float | None = 3.0,
        profile: str = "",
        history_path: str | None = None,
        min_history: int = history_mod.MIN_HISTORY,
    ):
        self.budget = budget
        self.max_spawns = max_spawns
        self.max_tokens = max_tokens
        self.timeout = timeout
        self.grace_period = grace_period
        self.pricing_table = pricing_table or pricing.DEFAULT_PRICING
        self.receipt_path = receipt_path
        self.quiet = quiet
        self.anomaly = anomaly
        self.profile = profile
        self.history_path = history_path
        self.min_history = min_history
        self._baseline: dict | None = None
        self.agent = ""

        self._lock = threading.Lock()
        self.usage = {"input": 0, "output": 0, "cache_write": 0, "cache_read": 0}
        self.spawns = 0
        self.num_turns = 0
        self.model = ""
        self.session_id = ""
        self.reported_cost: float | None = None
        self.breached = False
        self.breach_reason: str | None = None
        self.proc: subprocess.Popen | None = None
        self.started_at = 0.0

    # -- accounting -------------------------------------------------

    def _add_usage(self, obs: dict) -> None:
        # Trust boundary: usage comes from the child's own telemetry.
        # Clamp to >= 0 so a misbehaving child reporting negative usage
        # can never drive totals down and blind the caps.
        with self._lock:
            u = obs["usage"]
            for k in self.usage:
                self.usage[k] += max(0, u.get(k, 0))
            self.spawns += max(0, obs.get("spawns", 0))

    def estimated_cost(self) -> float:
        with self._lock:
            usage = dict(self.usage)
            model = self.model
        return pricing.estimate_cost(model, usage, self.pricing_table)

    def total_tokens(self) -> int:
        with self._lock:
            return sum(self.usage.values())

    # -- breach checks ----------------------------------------------

    def _check_caps(self) -> str | None:
        """Return a breach reason, or None if within guardrails."""
        if self.budget is not None and self.estimated_cost() > self.budget:
            return (
                f"budget exceeded: estimated ${self.estimated_cost():.4f} "
                f"> ${self.budget:.2f} cap"
            )
        if self.max_spawns is not None:
            with self._lock:
                spawns = self.spawns
            if spawns > self.max_spawns:
                return f"spawn cap exceeded: {spawns} subagents > {self.max_spawns} max"
        if self.max_tokens is not None and self.total_tokens() > self.max_tokens:
            return (
                f"token cap exceeded: {self.total_tokens():,} "
                f"> {self.max_tokens:,} max"
            )
        if self.anomaly is not None and self._baseline:
            b = self._baseline
            cost = self.estimated_cost()
            med_cost = b["median_cost"]
            if med_cost > 0 and cost > med_cost * self.anomaly:
                return (
                    f"anomaly: estimated ${cost:.4f} is "
                    f"{cost / med_cost:.1f}x the typical ${med_cost:.4f} "
                    f"across {b['runs']} past runs"
                )
            toks = self.total_tokens()
            med_toks = b["median_tokens"]
            if med_toks > 0 and toks > med_toks * self.anomaly:
                return (
                    f"anomaly: {toks:,} tokens is "
                    f"{toks / med_toks:.1f}x the typical {med_toks:,} "
                    f"across {b['runs']} past runs"
                )
        return None

    def _trip(self, reason: str) -> None:
        with self._lock:
            if self.breached:
                return
            self.breached = True
            self.breach_reason = reason
        self._kill_tree()

    # -- process control --------------------------------------------

    def _kill_tree(self) -> None:
        proc = self.proc
        if proc is None or proc.poll() is not None:
            return
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            return
        deadline = time.monotonic() + self.grace_period
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                return
            time.sleep(0.1)
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass

    # -- monitor -----------------------------------------------------

    def _monitor_stdout(self) -> None:
        assert self.proc is not None and self.proc.stdout is not None
        out = sys.stdout
        for line in self.proc.stdout:
            out.write(line)
            out.flush()
            try:
                # FAIL-CLOSED trust boundary: the child is untrusted. If a
                # telemetry line cannot be parsed cleanly (bad JSON, or a
                # numeric field that isn't numeric — e.g. "input_tokens":
                # "abc" raising ValueError), the run is tripped immediately.
                # Letting the exception escape would kill this thread and
                # leave the supervisor blocked on proc.wait() forever.
                obs = events.parse_line(line)
                if obs is not None:
                    obs = self._handle_obs(obs)
            except Exception as e:
                self._trip(
                    f"malformed telemetry: unparseable stream-json line "
                    f"({type(e).__name__}: {e}); failing closed"
                )
                break
            if self.breached:
                break

    def _handle_obs(self, obs: dict) -> dict:
        """Process one parsed observation. Returns the obs; trips on breach."""
        kind = obs["kind"]
        if kind == "init":
            with self._lock:
                self.model = obs.get("model") or self.model
                self.session_id = obs.get("session_id") or self.session_id
        elif kind == "usage":
            self._add_usage(obs)
            reason = self._check_caps()
            if reason:
                self._trip(reason)
        elif kind == "result":
            with self._lock:
                if obs.get("cost_usd") is not None:
                    # Child-reported cost: clamp to >= 0 for the same
                    # reason as usage (see _add_usage).
                    self.reported_cost = max(0.0, float(obs["cost_usd"]))
                self.num_turns = obs.get("num_turns") or self.num_turns
                if obs.get("session_id"):
                    self.session_id = obs["session_id"]
        return obs

    def _watchdog(self) -> None:
        if self.timeout is None:
            return
        deadline = self.started_at + self.timeout
        while True:
            proc = self.proc
            if proc is None or proc.poll() is not None:
                return
            if time.monotonic() >= deadline:
                self._trip(
                    f"time limit exceeded: {self.timeout:.0f}s wall clock"
                )
                return
            time.sleep(0.5)

    # -- main ---------------------------------------------------------

    def run(self, argv: list[str]) -> int:
        wall_start = time.time()
        # Monotonic clock for the watchdog (immune to wall-clock jumps).
        self.started_at = time.monotonic()
        self.agent = argv[0].split("/")[-1] if argv else ""
        if self.anomaly is not None:
            self._baseline = history_mod.baseline(
                self.agent, profile=self.profile,
                path=self.history_path, min_runs=self.min_history)
        self.proc = None
        try:
            self.proc = subprocess.Popen(
                argv,
                stdout=subprocess.PIPE,
                stderr=sys.stderr,
                text=True,
                bufsize=1,
                start_new_session=True,
            )
        except FileNotFoundError:
            # Clean error, exit 2 — no traceback, no receipt (no run happened).
            sys.stderr.write(
                f"shortleash: error: command not found: "
                f"{argv[0] if argv else ''}\n")
            sys.stderr.flush()
            return 2
        monitor = threading.Thread(target=self._monitor_stdout, daemon=True)
        monitor.start()
        watchdog = threading.Thread(target=self._watchdog, daemon=True)
        watchdog.start()

        exit_code = self.proc.wait()
        monitor.join(timeout=5)
        try:
            if self.proc.stdout:
                self.proc.stdout.close()
        except Exception:
            pass
        ended_at = time.time()

        if self.breached:
            exit_code = BREACH_EXIT_CODE

        cost_estimated = self.reported_cost is None
        cost = self.reported_cost if not cost_estimated else self.estimated_cost()

        rec = receipt_mod.build_receipt(
            command=argv,
            started_at=wall_start,
            ended_at=ended_at,
            breached=self.breached,
            breach_reason=self.breach_reason,
            cost_usd=cost,
            cost_estimated=cost_estimated,
            usage=dict(self.usage),
            num_turns=self.num_turns,
            spawns=self.spawns,
            exit_code=exit_code,
            model=self.model,
            session_id=self.session_id,
            agent=self.agent,
            profile=self.profile,
            baseline=self._baseline,
        )
        if self.receipt_path:
            receipt_mod.write_receipt(rec, self.receipt_path)
        # Learn from every run (breached runs are excluded from baselines).
        try:
            history_mod.record(rec, self.history_path)
        except OSError:
            pass
        if not self.quiet:
            sys.stderr.write(receipt_mod.format_human(rec) + "\n")
            sys.stderr.flush()
        return exit_code
