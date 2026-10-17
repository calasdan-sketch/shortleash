"""The guard: spawn the agent, tail its telemetry, kill it on breach.

Design:
- Child runs in its own process group (see proctree: start_new_session
  on POSIX, CREATE_NEW_PROCESS_GROUP on Windows) so a breach kill takes
  the whole tree, never our own group.
- A monitor thread reads the child's stdout line-by-line, parses
  stream-json events, passes every line through to our stdout
  (so the user still watches the agent work), and checks caps after
  each observation. If our stdout goes away (`| head -1`), echoing stops
  and parsing continues -- the caps still hold.
- On breach: kill the whole tree (SIGTERM, grace period, SIGKILL on
  POSIX; Job Object / taskkill /T /F on Windows).
- A watchdog thread enforces --timeout even when the child goes silent.
- Ctrl-C / SIGTERM to the leash: the child is in its own process group
  (and, on Windows, ignores console Ctrl-C), so the signal reaches only
  us. We kill the tree, write the receipt and exit 130 (Ctrl-C) or 143
  (SIGTERM) -- never die with a traceback leaving the agent running.
- Escaped grandchildren: when the child exits but something it spawned
  still holds our stdout pipe open, the monitor would block forever and
  the leash would never return. After the child exits we wait at most
  one grace period for the pipe to close, then kill the stragglers by
  process group and move on -- we never block on closing the pipe.
- Usage de-duplication: Claude Code emits one `assistant` event per
  content block, all carrying the same message id and the same usage.
  Usage is counted once per message id (growing fields are topped up,
  never added twice); spawns are counted once per tool_use block id.
- Exit code 124 on guard-triggered kill (mirrors timeout(1)); 125 when
  a telemetry-dependent cap was requested but the child never sent any
  usage telemetry (the cap could not have been enforced); 130/143 on
  Ctrl-C/SIGTERM; 1 when the agent's final result says is_error although
  the process exited 0; a child killed by signal N outside a breach is
  128+N like a shell; otherwise the child's exit code.
"""

from __future__ import annotations

import collections
import os
import signal
import subprocess
import sys
import threading
import time

from . import events
from . import history as history_mod
from . import launch
from . import pricing
from . import proctree
from . import receipt as receipt_mod

BREACH_EXIT_CODE = 124
#: A budget/token/spawn/anomaly cap was requested, but the child sent no
#: usage telemetry, so only the wall-clock limit (if any) was enforced.
NO_TELEMETRY_EXIT_CODE = 125
#: The leash itself was interrupted (Ctrl-C) / terminated (SIGTERM); the
#: tree was killed and a receipt written. 128 + signal, like a shell.
INTERRUPTED_EXIT_CODE = 130
TERMINATED_EXIT_CODE = 143
#: The process exited 0 but its stream-json result said is_error.
AGENT_ERROR_EXIT_CODE = 1

ANOMALY_HINT = ("stopped by anomaly mode, so this run is NOT in the baseline "
                "and would be stopped again: re-run without --anomaly to let "
                "it finish and be recorded, or use --profile NAME for a "
                "separate baseline")

_EXE_SUFFIXES = (".exe", ".cmd", ".bat", ".com", ".ps1")
#: How many message ids to remember for usage de-duplication. The split
#: events of one message are consecutive, so a small window is plenty.
_MESSAGE_WINDOW = 256


class _Terminated(BaseException):
    """Raised in the main thread by the SIGTERM handler to unwind wait()."""


def _child_stderr():
    """Where the child's stderr goes: our own sys.stderr when it is a real
    file (so fd-level redirection follows it), else inherit fd 2. A
    StringIO stderr (redirect_stderr, pytest --capture=sys) has no fileno
    and would make Popen raise."""
    try:
        sys.stderr.fileno()
    except (AttributeError, OSError, ValueError):
        return None
    return sys.stderr


def agent_name(argv0: str) -> str:
    """Basename of the agent binary without a Windows executable suffix
    ('C:\\tools\\claude.cmd' -> 'claude'), so history baselines and the
    receipt use the same key on every platform."""
    name = os.path.basename(argv0.replace("\\", "/"))
    low = name.lower()
    for suf in _EXE_SUFFIXES:
        if low.endswith(suf) and len(name) > len(suf):
            return name[: -len(suf)]
    return name


class Guard:
    def __init__(
        self,
        *,
        budget: float | None = None,
        max_spawns: int | None = None,
        max_tokens: int | None = None,
        timeout: float | None = None,
        grace_period: float = 5.0,
        pricing_table: pricing.PricingTable | None = None,
        receipt_path: str | None = None,
        quiet: bool = False,
        anomaly: float | None = None,
        profile: str = "",
        history_path: str | None = None,
        min_history: int = history_mod.MIN_HISTORY,
        redact_prompt: bool = False,
    ):
        self.budget = budget
        self.max_spawns = max_spawns
        self.max_tokens = max_tokens
        self.timeout = timeout
        self.grace_period = grace_period
        if pricing_table is None:
            pricing_table = pricing.PricingTable(rates=dict(pricing.DEFAULT_PRICING))
        self.pricing_table = pricing_table
        self.receipt_path = receipt_path
        self.quiet = quiet
        self.anomaly = anomaly
        self.profile = profile
        self.history_path = history_path
        self.min_history = min_history
        self.redact_prompt = redact_prompt
        self._baseline: dict | None = None
        self.agent = ""

        self._lock = threading.Lock()
        self.usage = {"input": 0, "output": 0, "cache_write": 0, "cache_read": 0}
        # message id -> the largest usage seen for it (de-duplication)
        self._seen_messages: collections.OrderedDict[str, dict] = \
            collections.OrderedDict()
        self._seen_spawns: set[str] = set()
        self.spawns = 0
        self.num_turns = 0
        self.model = ""
        self.session_id = ""
        self.reported_cost: float | None = None
        self.usage_events = 0
        self.agent_error = False
        self.agent_error_text = ""
        self.breached = False
        self.breach_reason: str | None = None
        self.interrupted_by: str | None = None  # "SIGINT" / "SIGTERM"
        self._in_wait = False
        self.escaped = False
        self.echo = True
        self.warnings: list[str] = []
        self.proc: subprocess.Popen | None = None
        self.job = None
        self.started_at = 0.0

    # -- accounting -------------------------------------------------

    def _add_usage(self, obs: dict) -> None:
        # Trust boundary: usage comes from the child's own telemetry.
        # Clamp to >= 0 so a misbehaving child reporting negative usage
        # can never drive totals down and blind the caps.
        with self._lock:
            u = {k: max(0, obs["usage"].get(k, 0)) for k in self.usage}
            mid = obs.get("message_id") or ""
            if mid and mid in self._seen_messages:
                # Same API response, next content block: usage repeats.
                # Count only growth (output tokens can still rise), never
                # the same tokens twice.
                prev = self._seen_messages[mid]
                for k in self.usage:
                    self.usage[k] += max(0, u[k] - prev[k])
                    prev[k] = max(prev[k], u[k])
                self._seen_messages.move_to_end(mid)
            else:
                for k in self.usage:
                    self.usage[k] += u[k]
                if mid:
                    self._seen_messages[mid] = dict(u)
                    while len(self._seen_messages) > _MESSAGE_WINDOW:
                        self._seen_messages.popitem(last=False)
            spawn_ids = obs.get("spawn_ids")
            if spawn_ids is None:
                self.spawns += max(0, obs.get("spawns", 0))
            else:
                for sid in spawn_ids:
                    key = f"{mid}:{sid}" if sid.startswith("<no-id>") else sid
                    if key not in self._seen_spawns:
                        self._seen_spawns.add(key)
                        self.spawns += 1
            self.usage_events += 1

    def estimated_cost(self) -> float:
        with self._lock:
            usage = dict(self.usage)
            model = self.model
        return pricing.estimate_cost(model, usage, self.pricing_table)

    def total_tokens(self) -> int:
        with self._lock:
            return sum(self.usage.values())

    @property
    def telemetry_seen(self) -> bool:
        """True once the child has reported any usage or a final cost."""
        with self._lock:
            return self.usage_events > 0 or self.reported_cost is not None

    def telemetry_caps(self) -> list[str]:
        """Caps that can only be enforced from the child's telemetry."""
        caps = []
        if self.budget is not None:
            caps.append("budget")
        if self.max_tokens is not None:
            caps.append("max_tokens")
        if self.max_spawns is not None:
            caps.append("max_spawns")
        if self.anomaly is not None and self._baseline:
            caps.append("anomaly")
        return caps

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
        if proc is None:
            return
        proctree.kill_tree(proc, grace=self.grace_period, job=self.job)

    # -- monitor -----------------------------------------------------

    def _echo_line(self, line: str) -> None:
        """Pass one child line through to our stdout. When stdout is gone
        (closed pipe, `| head -1`) stop echoing but keep parsing: the
        caps must hold whether or not anyone is watching."""
        if not self.echo:
            return
        try:
            out = sys.stdout
            out.write(line)
            out.flush()
        except (OSError, ValueError, AttributeError):
            self.echo = False
            self.warnings.append(
                "stdout closed while the agent was running; its output was "
                "no longer echoed (telemetry parsing continued)")
            self._silence_stdout()

    @staticmethod
    def _silence_stdout() -> None:
        """Point fd 1 at the null device so the interpreter's own flush at
        exit cannot fail with another BrokenPipeError."""
        try:
            fd = sys.stdout.fileno()
        except (AttributeError, OSError, ValueError):
            return
        try:
            devnull = os.open(os.devnull, os.O_WRONLY)
            os.dup2(devnull, fd)
            os.close(devnull)
        except OSError:
            pass

    def _monitor_stdout(self) -> None:
        assert self.proc is not None and self.proc.stdout is not None
        for line in self.proc.stdout:
            self._echo_line(line)
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
                if obs.get("is_error"):
                    self.agent_error = True
                    self.agent_error_text = (obs.get("error_text")
                                             or obs.get("subtype") or "")
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

    # -- escaped children ---------------------------------------------

    def _reap_stragglers(self, monitor: threading.Thread) -> None:
        """Called after the child exited. If the monitor is still blocked,
        a grandchild inherited our stdout pipe and is still running: kill
        the child's process group and give the pipe one more grace period
        to close. Never block on it beyond that."""
        monitor.join(timeout=self.grace_period)
        if not monitor.is_alive():
            return
        self.escaped = True
        killed = proctree.kill_tree(self.proc, grace=self.grace_period,
                                    child_exited=True, job=self.job)
        monitor.join(timeout=self.grace_period)
        if monitor.is_alive():
            self.warnings.append(
                "escaped child process(es) still hold the output pipe open "
                "after the kill (detached from the process group?) — "
                "the leash stopped waiting for them")
        elif killed:
            self.warnings.append(
                "the command left child process(es) running after it "
                "exited; they were killed with the process tree")
        else:
            self.warnings.append(
                "the command left child process(es) running after it "
                "exited; they ended on their own during the grace period")

    # -- signals -------------------------------------------------------

    def _on_sigterm(self, signum, frame):
        if self._in_wait:
            raise _Terminated()
        # Not inside the wait loop (launching, or already writing the
        # receipt): kill the tree now and let run() finish normally.
        self._interrupt("SIGTERM", "terminated (SIGTERM)")

    def _install_sigterm(self):
        """Route SIGTERM through the same path as Ctrl-C. Only the main
        thread may set handlers; elsewhere (Guard.run in a worker thread)
        the default action stands. Returns the previous handler or None."""
        if threading.current_thread() is not threading.main_thread():
            return None
        try:
            return signal.signal(signal.SIGTERM, self._on_sigterm)
        except (ValueError, OSError, AttributeError):
            return None

    @staticmethod
    def _restore_sigterm(previous) -> None:
        if previous is None:
            return
        try:
            signal.signal(signal.SIGTERM, previous)
        except (ValueError, OSError, AttributeError):
            pass

    def _wait_child(self) -> int | None:
        """Wait for the child, handling Ctrl-C and SIGTERM to the leash.

        Polls with a short timeout rather than wait() alone: on Windows an
        indefinite WaitForSingleObject is not interruptible, so Ctrl-C
        would only be noticed once the child exited on its own. After a
        kill the wait is bounded; None means the tree would not die.
        """
        proc = self.proc
        assert proc is not None
        deadline: float | None = None
        while True:
            if deadline is not None and time.monotonic() > deadline:
                self.warnings.append(
                    "the agent process did not exit after the kill; the "
                    "leash stopped waiting for it")
                return None
            self._in_wait = True
            try:
                return proc.wait(timeout=0.5)
            except subprocess.TimeoutExpired:
                continue
            except KeyboardInterrupt:
                self._interrupt("SIGINT", "interrupted (Ctrl-C)")
            except _Terminated:
                self._interrupt("SIGTERM", "terminated (SIGTERM)")
            finally:
                self._in_wait = False
            if deadline is None:
                deadline = time.monotonic() + self.grace_period + 10.0

    def _interrupt(self, which: str, reason: str) -> None:
        """The leash was signalled: trip, kill the tree, keep waiting. A
        second Ctrl-C during the grace period force-kills right away."""
        if self.interrupted_by is None:
            self.interrupted_by = which
        try:
            self._trip(f"{reason}: the leash was signalled and killed the "
                       f"agent's process tree")
        except (KeyboardInterrupt, _Terminated):
            self.grace_period = 0.0
            self._kill_tree()

    # -- main ---------------------------------------------------------

    def run(self, argv: list[str]) -> int:
        wall_start = time.time()
        # Monotonic clock for the watchdog (immune to wall-clock jumps).
        self.started_at = time.monotonic()
        self.agent = agent_name(argv[0]) if argv else ""
        if self.anomaly is not None:
            self._baseline = history_mod.baseline(
                self.agent, profile=self.profile,
                path=self.history_path, min_runs=self.min_history,
                warnings=self.warnings)
        self.proc = None
        self.job = None
        # Resolve the executable ourselves (PATHEXT shims on Windows) and
        # never hand prompt text to cmd.exe through a .cmd shim.
        try:
            cmd = launch.resolve_command(argv)
        except launch.LaunchError as e:
            sys.stderr.write(f"shortleash: error: {e}\n")
            sys.stderr.flush()
            return 2
        try:
            self.proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=_child_stderr(),
                # The agent streams UTF-8 JSON; never decode it with the
                # console code page (cp1252) and never die on a bad byte.
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                **proctree.popen_kwargs(),
            )
        except FileNotFoundError:
            # Clean error, exit 2 — no traceback, no receipt (no run happened).
            sys.stderr.write(
                f"shortleash: error: command not found: "
                f"{argv[0] if argv else ''}\n")
            sys.stderr.flush()
            return 2
        # Windows: the Job Object is what lets us kill grandchildren whose
        # parent already exited (None elsewhere, or if it could not be set
        # up -- then taskkill is the fallback).
        self.job = proctree.attach_job(self.proc)
        monitor = threading.Thread(target=self._monitor_stdout, daemon=True)
        monitor.start()
        watchdog = threading.Thread(target=self._watchdog, daemon=True)
        watchdog.start()

        previous_term = self._install_sigterm()
        try:
            exit_code = self._wait_child()
        finally:
            self._restore_sigterm(previous_term)
        self._reap_stragglers(monitor)
        if not monitor.is_alive():
            # Only close the pipe once the reader is done: closing a
            # buffered reader while another thread is blocked in read()
            # waits for that thread's lock — forever, if a detached
            # grandchild keeps the pipe open.
            try:
                if self.proc.stdout:
                    self.proc.stdout.close()
            except Exception:
                pass
        # Closing a kill-on-close job ends anything still inside it.
        proctree.close_job(self.job)
        ended_at = time.time()

        telemetry = self.telemetry_seen
        requested = self.telemetry_caps()
        enforced = (["timeout"] if self.timeout is not None else []) + (
            requested if telemetry else [])
        not_enforced = [] if telemetry else requested

        if exit_code is None:
            exit_code = BREACH_EXIT_CODE  # the tree would not die (warned)
        elif exit_code < 0:
            # POSIX: killed by signal N (outside a breach) -> 128+N, as a
            # shell would report it, instead of a negative number.
            exit_code = 128 - exit_code
        hint = None
        if self.breached:
            if self.interrupted_by == "SIGINT":
                exit_code = INTERRUPTED_EXIT_CODE
            elif self.interrupted_by == "SIGTERM":
                exit_code = TERMINATED_EXIT_CODE
            else:
                exit_code = BREACH_EXIT_CODE
            if (self.breach_reason or "").startswith("anomaly"):
                hint = ANOMALY_HINT
        elif self.agent_error and exit_code == 0:
            # The agent said the run failed even though it exited 0.
            exit_code = AGENT_ERROR_EXIT_CODE
        elif not telemetry and requested and exit_code == 0:
            # The child succeeded, but the caps the user asked for were
            # never checked against anything: say so in the exit code.
            exit_code = NO_TELEMETRY_EXIT_CODE

        cost_estimated = self.reported_cost is None
        if not telemetry:
            cost: float | None = None
        else:
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
            telemetry_seen=telemetry,
            usage_events=self.usage_events,
            enforced=enforced,
            not_enforced=not_enforced,
            pricing={"as_of": self.pricing_table.as_of,
                     "source": self.pricing_table.source},
            warnings=list(self.warnings),
            redact_prompt=self.redact_prompt,
            agent_error=self.agent_error,
            agent_error_text=self.agent_error_text,
            hint=hint,
        )
        if self.receipt_path:
            receipt_mod.write_receipt(rec, self.receipt_path)
        # Learn from every run that produced telemetry (breached runs are
        # excluded from baselines; telemetry-less runs say nothing about
        # what a normal run costs, so they are not recorded at all).
        if telemetry:
            try:
                history_mod.record(rec, self.history_path)
            except OSError:
                pass
        if not self.quiet:
            sys.stderr.write(receipt_mod.format_human(rec) + "\n")
            sys.stderr.flush()
        return exit_code
