"""Round 5 (final verification panel): regression tests, one class per finding.

H-1 A result-only stream (no live usage events) was never compared with a
    cap: `--budget 1` and a reported $50 ended "finished within guardrails".
H-2 SIGHUP / SIGQUIT (terminal closed, SSH dropped) killed the leash and left
    the agent, in its own session, running uncapped with no receipt.
M-3 The echo of the agent's output ran before the parse, in the same thread:
    a stalled reader of our stdout blinded every cap.
M-4 Pricing: a user override lost to the built-in substring match; unknown
    models were priced as Sonnet without a word; NaN / negative rates passed.
M-5 --budget nan / --timeout nan passed validation and could never trip.
M-6 A command that exists but cannot be started was a traceback.
L-7 A corrupt fleet.db / unusable state dir was a traceback; a non-finite
    reported cost wrote a bare `Infinity` into the receipt and the history.
L-8 Mutation survivors: the SIGKILL escalation, the cap comparisons, the
    result-only telemetry check were not pinned by any test.

Real-subprocess tests remove PYTHONUNBUFFERED from the child's environment
(it hides buffered-flush failures) and run the leash in its own session.
"""

import _isolate  # noqa: F401  (isolate ~/.shortleash before importing)

import errno
import io
import json
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

from shortleash import cli as cli_mod
from shortleash import fleet as fleet_mod
from shortleash import history as history_mod
from shortleash import pricing
from shortleash import proctree
from shortleash import receipt as receipt_mod
from shortleash.guard import (BREACH_EXIT_CODE, CANNOT_START_EXIT_CODE,
                              NO_TELEMETRY_EXIT_CODE, Guard, _EchoPump)

PY = sys.executable
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SIM = os.path.join(REPO, "sim", "runaway.py")
POSIX = not proctree.is_windows()


def _tmp() -> str:
    return tempfile.mkdtemp(prefix="sl5-")


def _child_env() -> dict:
    env = dict(os.environ)
    env.pop("PYTHONUNBUFFERED", None)
    return env


def result_line(cost=None, usage=None, **extra) -> str:
    ev = {"type": "result", "subtype": "success", "is_error": False,
          "result": "done", "num_turns": 1, "session_id": "s"}
    if cost is not None:
        ev["total_cost_usd"] = cost
    if usage is not None:
        ev["usage"] = usage
    ev.update(extra)
    return json.dumps(ev)


def printer(*lines: str) -> list:
    """A child that prints the given lines and exits 0."""
    return [PY, "-c", "import sys\nfor l in sys.argv[1:]: print(l, flush=True)",
            *lines]


def run_guard(guard: Guard, argv: list, quiet_out=True):
    tmp = _tmp()
    guard.receipt_path = os.path.join(tmp, "r.json")
    guard.history_path = os.path.join(tmp, "h.jsonl")
    err = io.StringIO()
    with redirect_stderr(err), redirect_stdout(io.StringIO()):
        code = guard.run(argv)
    with open(guard.receipt_path, encoding="utf-8") as f:
        rec = json.load(f)
    return code, rec, err.getvalue()


def pid_alive(pid: int) -> bool:
    if os.name == "nt":
        p = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"],
                           capture_output=True, text=True, timeout=30)
        return str(pid) in p.stdout
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def wait_dead(pid: int, seconds: float) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if not pid_alive(pid):
            return True
        time.sleep(0.05)
    return not pid_alive(pid)


def read_pid(path: str, seconds: float = 15.0) -> int:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        try:
            with open(path, encoding="utf-8") as f:
                txt = f.read().strip()
            if txt:
                return int(txt)
        except (OSError, ValueError):
            pass
        time.sleep(0.05)
    raise AssertionError("child never wrote its pid")


# ---------------------------------------------------------------------
# H-1

class TestFinalReportIsComparedWithTheCaps(unittest.TestCase):
    def test_result_only_stream_over_budget_is_a_breach(self):
        # the panel's repro: --budget 1, one result line reporting $50
        g = Guard(budget=1.0, quiet=True, anomaly=None)
        code, rec, _ = run_guard(g, printer(result_line(cost=50)))
        self.assertEqual(code, BREACH_EXIT_CODE)
        self.assertEqual(rec["exit_code"], BREACH_EXIT_CODE)
        self.assertTrue(rec["breached"])
        self.assertIn("budget exceeded", rec["breach_reason"])
        self.assertIn("after the fact", rec["breach_reason"])
        self.assertEqual(rec["cost_usd"], 50)
        self.assertFalse(rec["cost_estimated"])
        self.assertIs(rec["enforced_live"]["budget"], False)
        self.assertIs(rec["breach_live"], False)
        text = receipt_mod.format_human(rec)
        self.assertIn("EXCEEDED:", text)
        self.assertNotIn("STOPPED:", text)   # nothing was stopped
        self.assertNotIn("within guardrails", text)
        self.assertNotIn(chr(0x2713), text)

    def test_exactly_at_the_cap_is_not_a_breach(self):
        g = Guard(budget=1.0, quiet=True, anomaly=None)
        code, rec, _ = run_guard(g, printer(result_line(cost=1.0)))
        self.assertEqual(code, 0)
        self.assertFalse(rec["breached"])

    def test_result_only_stream_within_budget_says_it_was_not_live(self):
        g = Guard(budget=1.0, quiet=True, anomaly=None)
        code, rec, _ = run_guard(g, printer(result_line(cost=0.5)))
        self.assertEqual(code, 0)
        self.assertFalse(rec["breached"])
        self.assertTrue(rec["telemetry_seen"])
        self.assertEqual(rec["enforced"], ["budget"])
        self.assertEqual(rec["enforced_live"], {"budget": False})
        self.assertIsNone(rec["breach_live"])
        self.assertTrue(any("after the run" in w for w in rec["warnings"]),
                        rec["warnings"])
        text = receipt_mod.format_human(rec)
        self.assertIn("finished within guardrails", text)
        self.assertIn("after the run", text)

    def test_live_estimate_under_the_cap_but_final_report_over_it(self):
        usage_event = json.dumps({"type": "assistant", "message": {
            "id": "m1", "content": [],
            "usage": {"input_tokens": 1000, "output_tokens": 100}}})
        g = Guard(budget=1.0, quiet=True, anomaly=None)
        code, rec, _ = run_guard(g, printer(usage_event, result_line(cost=5)))
        self.assertEqual(code, BREACH_EXIT_CODE)
        self.assertIn("after the fact", rec["breach_reason"])
        self.assertIn("live estimate", rec["breach_reason"])
        self.assertIs(rec["enforced_live"]["budget"], True)  # it could have
        self.assertIs(rec["breach_live"], False)             # but it did not

    def test_token_cap_is_checked_against_the_result_usage(self):
        g = Guard(max_tokens=1000, quiet=True, anomaly=None)
        code, rec, _ = run_guard(g, printer(result_line(
            usage={"input_tokens": 600000, "output_tokens": 10})))
        self.assertEqual(code, BREACH_EXIT_CODE)
        self.assertIn("token cap exceeded", rec["breach_reason"])
        self.assertIn("after the fact", rec["breach_reason"])
        self.assertEqual(rec["tokens"]["input"], 600000)
        self.assertIs(rec["enforced_live"]["max_tokens"], False)

    def test_a_final_report_between_one_and_two_times_the_budget_is_a_breach(self):
        # a comparison that is too lenient by a factor lets these through
        for cost in (1.0001, 1.01, 1.5, 1.99):
            with self.subTest(cost=cost):
                g = Guard(budget=1.0, quiet=True, anomaly=None)
                code, rec, _ = run_guard(g, printer(result_line(cost=cost)))
                self.assertEqual(code, BREACH_EXIT_CODE)
                self.assertTrue(rec["breached"])
                self.assertIn("budget exceeded after the fact",
                              rec["breach_reason"])

    def test_the_token_cap_is_exact_for_a_final_report(self):
        for total, over in ((999, False), (1000, False), (1001, True),
                            (1500, True), (1999, True)):
            with self.subTest(total=total):
                g = Guard(max_tokens=1000, quiet=True, anomaly=None)
                code, rec, _ = run_guard(g, printer(result_line(
                    usage={"input_tokens": total - 10, "output_tokens": 10})))
                self.assertEqual(rec["breached"], over)
                self.assertEqual(code, BREACH_EXIT_CODE if over else 0)
                if over:
                    self.assertIn("token cap exceeded after the fact",
                                  rec["breach_reason"])

    def test_spawn_cap_cannot_be_enforced_without_assistant_events(self):
        g = Guard(max_spawns=1, quiet=True, anomaly=None)
        code, rec, _ = run_guard(g, printer(result_line(cost=0.1)))
        self.assertEqual(rec["not_enforced"], ["max_spawns"])
        self.assertEqual(code, NO_TELEMETRY_EXIT_CODE)

    def test_non_finite_reported_cost_fails_closed(self):
        g = Guard(budget=5.0, quiet=True, anomaly=None)
        code, rec, _ = run_guard(g, printer(
            '{"type":"result","total_cost_usd":1e999,"is_error":false}'))
        self.assertEqual(code, BREACH_EXIT_CODE)
        self.assertIn("malformed telemetry", rec["breach_reason"])
        self.assertNotIn("Infinity", json.dumps(rec))
        with open(g.receipt_path, encoding="utf-8") as f:
            raw = f.read()
        self.assertNotIn("Infinity", raw)
        self.assertNotIn("NaN", raw)
        if os.path.exists(g.history_path):   # nothing usable was reported
            with open(g.history_path, encoding="utf-8") as f:
                self.assertNotIn("Infinity", f.read())

    def test_cli_end_to_end_the_panels_repro(self):
        tmp = _tmp()
        rp = os.path.join(tmp, "r.json")
        err = io.StringIO()
        with redirect_stderr(err), redirect_stdout(io.StringIO()):
            code = cli_mod.main(["run", "--budget", "1", "--receipt", rp,
                                 "--history-path", os.path.join(tmp, "h"),
                                 "--"] + printer(result_line(cost=50)))
        self.assertEqual(code, BREACH_EXIT_CODE)
        self.assertIn("EXCEEDED:", err.getvalue())
        self.assertNotIn("finished within guardrails", err.getvalue())

    def test_live_runs_are_unchanged(self):
        # a well-behaved stream with live usage: still live, still the tick
        g = Guard(budget=10.0, quiet=True, anomaly=None)
        code, rec, _ = run_guard(g, [PY, SIM, "--well-behaved"])
        self.assertEqual(code, 0)
        self.assertEqual(rec["enforced_live"], {"budget": True})
        self.assertIsNone(rec["breach_live"])
        self.assertFalse(any("after the run" in w for w in rec["warnings"]))
        self.assertIn("finished within guardrails",
                      receipt_mod.format_human(rec))
        # and a live breach is still STOPPED, with breach_live true
        g = Guard(budget=0.05, quiet=True, anomaly=None, grace_period=1.0)
        code, rec, _ = run_guard(g, [PY, SIM, "--turns", "30",
                                     "--tokens-per-turn", "15000",
                                     "--delay", "0.05"])
        self.assertEqual(code, BREACH_EXIT_CODE)
        self.assertIs(rec["breach_live"], True)
        self.assertIn("STOPPED:", receipt_mod.format_human(rec))


# ---------------------------------------------------------------------
# H-2

SLEEPING_AGENT = (
    "import os, sys, time, json; "
    "open(sys.argv[1], 'w', encoding='utf-8').write(str(os.getpid())); "
    "sys.stdout.write(json.dumps({'type': 'system', 'subtype': 'init', "
    "'model': 'claude-sonnet-4-5', 'session_id': 'x'}) + chr(10)); "
    "sys.stdout.flush(); time.sleep(120)")


def _leash(*args: str, stdout=subprocess.DEVNULL, stderr=None, preexec=None):
    """The leash as a real subprocess in its own session, so a signal aimed
    at it never reaches this test process."""
    return subprocess.Popen([PY, "-m", "shortleash", "run", *args], cwd=REPO,
                            stdout=stdout, stderr=stderr, env=_child_env(),
                            start_new_session=True, preexec_fn=preexec)


@unittest.skipUnless(POSIX, "POSIX signals")
class TestHangupAndQuitToTheLeash(unittest.TestCase):
    def _start(self, *extra, preexec=None):
        tmp = _tmp()
        self.pidfile = os.path.join(tmp, "agent.pid")
        self.rp = os.path.join(tmp, "r.json")
        self.errpath = os.path.join(tmp, "err.txt")
        self.errf = open(self.errpath, "w", encoding="utf-8")
        self.addCleanup(self.errf.close)
        p = _leash("--timeout", "600", "--grace-period", "1",
                   "--history-path", os.path.join(tmp, "h.jsonl"),
                   "--receipt", self.rp, *extra, "--", PY, "-c",
                   SLEEPING_AGENT, self.pidfile, stderr=self.errf,
                   preexec=preexec)
        self.agent = read_pid(self.pidfile)
        time.sleep(0.4)  # let the leash reach its wait loop
        self.addCleanup(self._cleanup, p)
        return p

    def _cleanup(self, p):
        if p.poll() is None:
            try:
                os.killpg(p.pid, signal.SIGKILL)
            except OSError:
                p.kill()
        if pid_alive(self.agent):
            try:
                os.kill(self.agent, signal.SIGKILL)
            except OSError:
                pass

    def _receipt(self):
        with open(self.rp, encoding="utf-8") as f:
            return json.load(f)

    def _check(self, sig, code, name):
        p = self._start()
        os.kill(p.pid, sig)
        try:
            rc = p.wait(timeout=30)
        except subprocess.TimeoutExpired:
            self.fail(f"the leash did not return after {name}")
        self.assertEqual(rc, code)
        self.assertTrue(wait_dead(self.agent, 10),
                        f"the agent kept running after the leash got {name}")
        rec = self._receipt()
        self.assertTrue(rec["breached"])
        self.assertIn(name, rec["breach_reason"])
        self.assertEqual(rec["exit_code"], code)
        with open(self.errpath, encoding="utf-8", errors="replace") as f:
            self.assertNotIn("Traceback", f.read())

    def test_sighup_kills_the_tree_writes_the_receipt_exit_129(self):
        self._check(signal.SIGHUP, 129, "SIGHUP")

    def test_sigquit_kills_the_tree_writes_the_receipt_exit_131(self):
        self._check(signal.SIGQUIT, 131, "SIGQUIT")

    def test_a_hangup_that_the_parent_chose_to_ignore_stays_ignored(self):
        # `nohup shortleash run ...`: the run must survive the terminal
        def ignore_hup():
            signal.signal(signal.SIGHUP, signal.SIG_IGN)
        p = self._start(preexec=ignore_hup)
        os.kill(p.pid, signal.SIGHUP)
        time.sleep(1.0)
        self.assertIsNone(p.poll(), "nohup'd leash died of SIGHUP")
        self.assertTrue(pid_alive(self.agent))
        os.kill(p.pid, signal.SIGTERM)
        self.assertEqual(p.wait(timeout=30), 143)

    @unittest.skipUnless(sys.platform.startswith("linux"), "PR_SET_PDEATHSIG")
    def test_sigkill_of_the_leash_takes_the_agent_with_it(self):
        # SIGKILL cannot be handled; the kernel can still end the agent
        p = self._start()
        os.kill(p.pid, signal.SIGKILL)
        p.wait(timeout=30)
        self.assertTrue(wait_dead(self.agent, 10),
                        "the agent outlived a SIGKILLed leash")


class TestSignalExitCodes(unittest.TestCase):
    def test_signal_names_map_to_shell_style_codes(self):
        from shortleash import guard as guard_mod
        codes = guard_mod.SIGNAL_EXIT_CODES
        self.assertEqual(codes["SIGINT"], 130)
        self.assertEqual(codes["SIGTERM"], 143)
        self.assertEqual(codes["SIGHUP"], 129)
        self.assertEqual(codes["SIGQUIT"], 131)
        self.assertEqual(codes["SIGBREAK"], 143)   # Windows Ctrl-Break

    def test_handlers_are_installed_only_for_signals_that_exist(self):
        g = Guard(quiet=True)
        seen = []
        with mock.patch.object(signal, "signal",
                               lambda n, h: seen.append(n) or signal.SIG_DFL):
            with mock.patch.object(signal, "getsignal",
                                   lambda n: signal.SIG_DFL):
                g._install_handlers()
        for name in ("SIGINT", "SIGTERM", "SIGHUP", "SIGQUIT", "SIGBREAK"):
            num = getattr(signal, name, None)
            self.assertEqual(num in seen, num is not None, name)


class TestParentDeathKwargs(unittest.TestCase):
    def test_linux_gets_a_preexec_hook_elsewhere_nothing(self):
        for platform in ("win32", "darwin", "freebsd14"):
            with mock.patch.object(sys, "platform", platform):
                self.assertEqual(proctree.parent_death_kwargs(), {}, platform)
        if sys.platform.startswith("linux"):
            kw = proctree.parent_death_kwargs()
            self.assertEqual(list(kw), ["preexec_fn"])
            self.assertTrue(callable(kw["preexec_fn"]))


class TestTheSummaryCannotBreakTheExitCode(unittest.TestCase):
    def test_a_stderr_that_is_gone_does_not_turn_into_a_traceback(self):
        # SIGHUP means the terminal is gone: writing the summary to it fails
        # (EIO). The receipt file and the exit code are the verdict.
        class Gone:
            def write(self, _text):
                raise OSError(errno.EIO, "Input/output error")

            def flush(self):
                raise OSError(errno.EIO, "Input/output error")

            def fileno(self):
                raise OSError(errno.EIO, "Input/output error")

        tmp = _tmp()
        g = Guard(budget=1.0, anomaly=None)
        g.receipt_path = os.path.join(tmp, "r.json")
        g.history_path = os.path.join(tmp, "h.jsonl")
        with mock.patch.object(sys, "stderr", Gone()), \
                redirect_stdout(io.StringIO()):
            code = g.run(printer(result_line(cost=50)))
        self.assertEqual(code, BREACH_EXIT_CODE)
        with open(g.receipt_path, encoding="utf-8") as f:
            self.assertTrue(json.load(f)["breached"])


# ---------------------------------------------------------------------
# M-3

#: 300 KB of chatter (far more than a pipe holds), then one usage event worth
#: $15 on an opus model, then the agent just keeps running.
CHATTY_SPENDER = (
    "import json, os, sys, time\n"
    "open(sys.argv[1], 'w', encoding='utf-8').write(str(os.getpid()))\n"
    "for i in range(3000):\n"
    "    sys.stdout.write('chatter line %05d ' % i + 'x' * 90 + chr(10))\n"
    "sys.stdout.flush()\n"
    "print(json.dumps({'type': 'system', 'subtype': 'init', "
    "'model': 'claude-opus-4', 'session_id': 's'}), flush=True)\n"
    "print(json.dumps({'type': 'assistant', 'message': {'id': 'm1', "
    "'content': [], 'usage': {'input_tokens': 1000000, "
    "'output_tokens': 0}}}), flush=True)\n"
    "time.sleep(120)\n")


class TestAStalledReaderDoesNotBlindTheCaps(unittest.TestCase):
    def test_breach_fires_on_time_while_nobody_reads_our_stdout(self):
        tmp = _tmp()
        pidfile = os.path.join(tmp, "agent.pid")
        rp = os.path.join(tmp, "r.json")
        # stdout is a pipe that this test never reads: the leash's echo of
        # the chatter fills it and blocks.
        leash = subprocess.Popen(
            [PY, "-m", "shortleash", "run", "--budget", "1", "--quiet",
             "--grace-period", "1", "--receipt", rp,
             "--history-path", os.path.join(tmp, "h.jsonl"), "--",
             PY, "-c", CHATTY_SPENDER, pidfile],
            cwd=REPO, env=_child_env(), stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, start_new_session=True)
        agent = None
        rc = None
        try:
            agent = read_pid(pidfile)
            started = time.monotonic()
            self.assertTrue(
                wait_dead(agent, 20),
                "the agent was still running 20 s after it reported $15 "
                "against a $1 budget: a reader that stalls blinded the caps")
            self.assertLess(time.monotonic() - started, 12)
            deadline = time.monotonic() + 15
            while not os.path.exists(rp) and time.monotonic() < deadline:
                time.sleep(0.05)
            with open(rp, encoding="utf-8") as f:
                rec = json.load(f)
            self.assertTrue(rec["breached"])
            self.assertIn("budget exceeded", rec["breach_reason"])
            self.assertIs(rec["breach_live"], True)
        finally:
            # let the leash finish: closing our end ends its blocked write
            leash.stdout.close()
            try:
                rc = leash.wait(timeout=30)
            except subprocess.TimeoutExpired:
                leash.kill()
            if agent is not None and pid_alive(agent):
                try:
                    os.kill(agent, signal.SIGKILL if POSIX else signal.SIGTERM)
                except OSError:
                    pass
        self.assertEqual(rc, BREACH_EXIT_CODE)


class TestEchoPump(unittest.TestCase):
    def test_lines_come_out_in_order_and_close_flushes_them(self):
        seen = []
        pump = _EchoPump(seen.append)
        for i in range(200):
            pump.put("line %d\n" % i)
        self.assertTrue(pump.close(stall=5.0))
        self.assertEqual(seen, ["line %d\n" % i for i in range(200)])
        self.assertEqual(pump.dropped, 0)

    def test_a_blocked_writer_never_blocks_put_and_the_queue_is_bounded(self):
        release = threading.Event()
        pump = _EchoPump(lambda _line: release.wait(30), limit=1000)
        started = time.monotonic()
        for _ in range(5000):
            pump.put("x" * 99 + "\n")
        self.assertLess(time.monotonic() - started, 5.0,
                        "put() waited for the stalled writer")
        self.assertGreater(pump.dropped, 4000)
        self.assertLessEqual(pump.pending(), 11)
        self.assertFalse(pump.close(stall=0.3), "a stuck writer cannot drain")
        release.set()

    def test_close_keeps_waiting_while_lines_keep_going_out(self):
        seen = []

        def slow(line):
            time.sleep(0.1)
            seen.append(line)
        pump = _EchoPump(slow)
        for i in range(12):                   # 1.2 s in all, stall limit 0.5 s
            pump.put("%d\n" % i)
        self.assertTrue(pump.close(stall=0.5))
        self.assertEqual(len(seen), 12)

    def test_an_oversized_line_is_still_accepted_when_the_queue_is_empty(self):
        seen = []
        pump = _EchoPump(seen.append, limit=10)
        pump.put("y" * 1000 + "\n")
        self.assertTrue(pump.close(stall=2.0))
        self.assertEqual(len(seen), 1)


class TestAGuardWhoseStdoutIsStuck(unittest.TestCase):
    def test_the_receipt_says_what_was_not_echoed_and_the_run_ends(self):
        release = threading.Event()
        self.addCleanup(release.set)

        def stuck(_self, _line):
            release.wait(60)

        lines = ["chatter %d" % i for i in range(50)]
        g = Guard(budget=1.0, quiet=True, anomaly=None)
        keep = sys.stdout
        with mock.patch.object(Guard, "_echo_line", stuck), \
                mock.patch.object(sys, "stdout", io.StringIO()):
            started = time.monotonic()
            code, rec, _ = run_guard(g, printer(*lines, result_line(cost=0.5)))
            took = time.monotonic() - started
            swapped = sys.stdout
        self.assertIsNot(swapped, keep)
        self.assertEqual(code, 0)
        self.assertLess(took, 20)
        self.assertTrue(any("not keeping up" in w for w in rec["warnings"]),
                        rec["warnings"])
        self.assertTrue(any("not echoed" in w for w in rec["warnings"]))
        # the cap was checked on every line regardless
        self.assertEqual(rec["cost_usd"], 0.5)


# ---------------------------------------------------------------------
# M-4

ONE_M_INPUT = {"input": 1000000, "output": 0, "cache_write": 0, "cache_read": 0}


def _pricing_file(text_or_obj) -> str:
    path = os.path.join(_tmp(), "pricing.json")
    with open(path, "w", encoding="utf-8") as f:
        f.write(text_or_obj if isinstance(text_or_obj, str)
                else json.dumps(text_or_obj))
    return path


class TestPricingLookup(unittest.TestCase):
    def _cost(self, model, table):
        return pricing.estimate_cost(model, ONE_M_INPUT, table)

    def test_a_specific_override_beats_the_builtin_family_rate(self):
        # the panel's repro: opus-4-5 priced at 15 although the file says 5
        t = pricing.load_table(_pricing_file({"opus-4-5": [5, 25]}), environ={})
        self.assertAlmostEqual(self._cost("claude-opus-4-5", t), 5.0)
        self.assertEqual(pricing._rate_for("claude-opus-4-5", t.rates),
                         (5.0, 25.0))
        self.assertAlmostEqual(self._cost("claude-opus-4-1", t), 15.0)

    def test_an_override_with_a_shorter_key_still_beats_the_builtin_table(self):
        # "all claude models cost 1/5" is what the file says, whatever the
        # built-in table thinks of opus
        t = pricing.load_table(_pricing_file({"claude": [1, 5]}), environ={})
        self.assertAlmostEqual(self._cost("claude-opus-4-1", t), 1.0)
        self.assertAlmostEqual(self._cost("claude-haiku-4", t), 1.0)

    def test_the_longest_matching_key_wins(self):
        t = pricing.load_table(
            _pricing_file({"claude": [1, 5], "opus-4-1": [2, 10]}), environ={})
        self.assertAlmostEqual(self._cost("claude-opus-4-1", t), 2.0)
        plain = {"sonnet": (3.0, 15.0), "sonnet-4-5": (9.0, 9.0),
                 "default": (3.0, 15.0)}
        self.assertEqual(pricing._rate_for("claude-sonnet-4-5", plain),
                         (9.0, 9.0))
        self.assertEqual(pricing._rate_for("claude-sonnet-3", plain),
                         (3.0, 15.0))

    def test_which_entry_priced_a_model(self):
        t = pricing.load_table(None, environ={})
        self.assertEqual(pricing.match("claude-opus-4-1", t)[0], "opus")
        self.assertEqual(pricing.match("mystery-model-9", t)[0], "default")
        self.assertEqual(pricing.match("", t)[0], "default")

    def test_non_finite_and_negative_rates_are_refused(self):
        for text in ('{"sonnet": [NaN, 1]}', '{"sonnet": [1, NaN]}',
                     '{"sonnet": [Infinity, 1]}', '{"sonnet": [1, -Infinity]}',
                     '{"sonnet": [1, 1e999]}', '{"sonnet": [-1, 5]}',
                     '{"sonnet": [1, -5]}', '{"default": [-0.5, 1]}'):
            with self.assertRaises(ValueError, msg=text) as cm:
                pricing.load_table(_pricing_file(text), environ={})
            self.assertIn("sonnet" if "sonnet" in text else "default",
                          str(cm.exception))

    def test_zero_rates_are_allowed_and_an_empty_key_is_not(self):
        t = pricing.load_table(_pricing_file({"free-model": [0, 0]}),
                               environ={})
        self.assertAlmostEqual(self._cost("free-model-1", t), 0.0)
        with self.assertRaises(ValueError):
            pricing.load_table(_pricing_file({"": [1, 1]}), environ={})

    def test_a_bad_pricing_file_is_a_clean_exit_2(self):
        err = io.StringIO()
        with redirect_stderr(err), redirect_stdout(io.StringIO()):
            code = cli_mod.main(["run", "--pricing",
                                 _pricing_file('{"sonnet": [NaN, 1]}'),
                                 "--"] + printer("x"))
        self.assertEqual(code, 2)
        self.assertIn("pricing", err.getvalue())
        self.assertNotIn("Traceback", err.getvalue())


def usage_run(model, table=None):
    init = json.dumps({"type": "system", "subtype": "init", "model": model,
                       "session_id": "s"})
    msg = json.dumps({"type": "assistant", "message": {
        "id": "m1", "content": [],
        "usage": {"input_tokens": 1000, "output_tokens": 100}}})
    g = Guard(budget=50.0, quiet=True, anomaly=None, pricing_table=table)
    return run_guard(g, printer(init, msg))


class TestAnAssumedPriceIsSaid(unittest.TestCase):
    def test_an_unknown_model_is_flagged_in_the_receipt(self):
        code, rec, _ = usage_run("mystery-model-9")
        self.assertEqual(code, 0)
        self.assertIs(rec["price_assumed"], True)
        self.assertTrue(any("mystery-model-9" in w and "fallback" in w
                            for w in rec["warnings"]), rec["warnings"])

    def test_a_known_model_is_not(self):
        code, rec, _ = usage_run("claude-sonnet-4-5")
        self.assertIs(rec["price_assumed"], False)
        self.assertFalse(any("fallback" in w for w in rec["warnings"]))

    def test_a_model_the_users_file_prices_is_not(self):
        t = pricing.load_table(_pricing_file({"mystery": [1, 1]}), environ={})
        code, rec, _ = usage_run("mystery-model-9", t)
        self.assertIs(rec["price_assumed"], False)

    def test_no_model_at_all_is_flagged_too(self):
        code, rec, _ = usage_run("")
        self.assertIs(rec["price_assumed"], True)

    def test_a_run_without_usage_has_nothing_to_flag(self):
        g = Guard(timeout=30.0, quiet=True, anomaly=None)
        code, rec, _ = run_guard(g, printer("hello"))
        self.assertIs(rec["price_assumed"], False)

    def test_the_notice_is_printed_once_on_stderr_unless_quiet(self):
        init = json.dumps({"type": "system", "subtype": "init",
                           "model": "mystery-model-9", "session_id": "s"})
        msgs = [json.dumps({"type": "assistant", "message": {
            "id": "m%d" % i, "content": [],
            "usage": {"input_tokens": 10, "output_tokens": 1}}})
            for i in range(5)]
        g = Guard(budget=50.0, anomaly=None)          # not quiet
        code, rec, err = run_guard(g, printer(init, *msgs))
        self.assertEqual(err.count("[shortleash] warning:"), 1, err)  # live, once
        self.assertEqual(err.count("is not in the pricing table"), 2, err)  # + receipt


# ---------------------------------------------------------------------
# M-5

class TestCapsMustBeFiniteNumbers(unittest.TestCase):
    def _refused(self, *args):
        err, out = io.StringIO(), io.StringIO()
        with redirect_stderr(err), redirect_stdout(out):
            with self.assertRaises(SystemExit) as cm:
                cli_mod.main(["run", *args, "--"] + printer("must-not-run"))
        self.assertEqual(cm.exception.code, 2)
        self.assertNotIn("must-not-run", out.getvalue(), "the command ran")
        return err.getvalue()

    def test_nan_and_infinity_are_refused_for_every_float_option(self):
        for flag in ("--budget", "--timeout", "--grace-period", "--anomaly"):
            for bad in ("nan", "NaN", "inf", "-inf", "1e999"):
                text = self._refused(flag + "=" + bad)
                self.assertIn(flag, text)
                self.assertIn("finite", text, flag + " " + bad)

    def test_the_panels_repro_runs_nothing(self):
        text = self._refused("--budget", "nan")
        self.assertIn("--budget must be a finite number", text)

    def test_a_negative_grace_period_is_refused(self):
        self.assertIn("--grace-period must be >= 0",
                      self._refused("--grace-period", "-1"))

    def test_ordinary_values_still_pass(self):
        for args in (["--budget", "0"], ["--budget", "2.5"],
                     ["--timeout", "30"], ["--grace-period", "0"],
                     ["--anomaly", "5"]):
            tmp = _tmp()
            err = io.StringIO()
            with redirect_stderr(err), redirect_stdout(io.StringIO()):
                code = cli_mod.main(
                    ["run", "--quiet", "--history-path",
                     os.path.join(tmp, "h")] + args + ["--"]
                    + printer("hello"))
            self.assertIn(code, (0, NO_TELEMETRY_EXIT_CODE), args)


# ---------------------------------------------------------------------
# M-6

class TestACommandThatCannotBeStarted(unittest.TestCase):
    def _cli(self, argv, *flags):
        tmp = _tmp()
        rp = os.path.join(tmp, "r.json")
        err = io.StringIO()
        with redirect_stderr(err), redirect_stdout(io.StringIO()):
            code = cli_mod.main(["run", "--receipt", rp, "--history-path",
                                 os.path.join(tmp, "h"), *flags, "--"] + argv)
        text = err.getvalue()
        self.assertNotIn("Traceback", text)
        self.assertFalse(os.path.exists(rp), "no run happened: no receipt")
        return code, text

    def test_the_exit_code_is_the_shells_126(self):
        self.assertEqual(CANNOT_START_EXIT_CODE, 126)

    def test_a_directory_is_a_clean_error(self):
        # the panel's repro: `shortleash run -- /tmp`
        code, text = self._cli([_tmp()])
        self.assertEqual(code, CANNOT_START_EXIT_CODE)
        self.assertIn("shortleash: error: cannot start", text)

    @unittest.skipUnless(POSIX, "needs POSIX permission bits")
    def test_a_file_without_the_execute_bit(self):
        path = os.path.join(_tmp(), "noexec.sh")
        with open(path, "w", encoding="utf-8") as f:
            f.write("echo hi")
        os.chmod(path, 0o644)
        code, text = self._cli([path])
        self.assertEqual(code, CANNOT_START_EXIT_CODE)

    def test_a_file_that_is_not_an_executable(self):
        name = "junk.exe" if os.name == "nt" else "junk"
        path = os.path.join(_tmp(), name)
        with open(path, "wb") as f:
            f.write(bytes(range(1, 64)))
        os.chmod(path, 0o755)
        code, text = self._cli([path])
        self.assertEqual(code, CANNOT_START_EXIT_CODE)
        self.assertIn("cannot start", text)

    def test_a_path_through_a_file_is_a_clean_error_too(self):
        path = os.path.join(_tmp(), "afile")
        with open(path, "w", encoding="utf-8") as f:
            f.write("x")
        code, text = self._cli([os.path.join(path, "inside")])
        self.assertIn(code, (CANNOT_START_EXIT_CODE, 2))   # ENOTDIR / ENOENT
        self.assertIn("shortleash: error:", text)

    def test_not_found_keeps_exit_2(self):
        code, text = self._cli(["definitely-not-a-command-sl5"])
        self.assertEqual(code, 2)
        self.assertIn("command not found", text)


# ---------------------------------------------------------------------
# L-7

def _strict_loads(text: str):
    """json.loads that refuses the non-standard NaN / Infinity tokens."""
    def refuse(token):
        raise ValueError("non-standard JSON token: " + token)
    return json.loads(text, parse_constant=refuse)


def _db_in_the_way() -> str:
    """A path for fleet.db whose parent is a regular file: the state dir
    cannot be created (the same failure as a read-only home, without needing
    permissions)."""
    blocker = os.path.join(_tmp(), "not-a-dir")
    with open(blocker, "w", encoding="utf-8") as f:
        f.write("x")
    return os.path.join(blocker, "fleet.db")


def _garbage_db() -> str:
    path = os.path.join(_tmp(), "fleet.db")
    with open(path, "wb") as f:
        f.write(b"this is not a sqlite database" * 100)
    return path


class TestAnUnusableStateDirIsNotATraceback(unittest.TestCase):
    def _fleet_list(self, db):
        err, out = io.StringIO(), io.StringIO()
        with mock.patch.object(fleet_mod, "DB_PATH", db), \
                redirect_stderr(err), redirect_stdout(out):
            code = cli_mod.main(["fleet", "list"])
        return code, err.getvalue()

    def test_fleet_list_with_a_corrupt_database(self):
        code, err = self._fleet_list(_garbage_db())
        self.assertEqual(code, 1)
        self.assertIn("shortleash: error:", err)
        self.assertIn("fleet", err)
        self.assertNotIn("Traceback", err)

    def test_fleet_list_when_the_state_dir_cannot_be_created(self):
        code, err = self._fleet_list(_db_in_the_way())
        self.assertEqual(code, 1)
        self.assertIn("shortleash: error:", err)
        self.assertNotIn("Traceback", err)

    def test_a_fleet_run_goes_ahead_without_a_claim(self):
        # a lock must never be a deadlock: no claim is a warning, not a stop
        for db in (_garbage_db(), _db_in_the_way()):
            tmp = _tmp()
            rp = os.path.join(tmp, "r.json")
            err, out = io.StringIO(), io.StringIO()
            with mock.patch.object(fleet_mod, "DB_PATH", db), \
                    redirect_stderr(err), redirect_stdout(out):
                code = cli_mod.main(
                    ["run", "--fleet", "--quiet", "--receipt", rp,
                     "--history-path", os.path.join(tmp, "h"), "--"]
                    + printer("the-agent-ran"))
            self.assertIn("the-agent-ran", out.getvalue())
            self.assertIn(code, (0, NO_TELEMETRY_EXIT_CODE))
            self.assertIn("fleet", err.getvalue())
            self.assertIn("WITHOUT a claim", err.getvalue())
            self.assertNotIn("Traceback", err.getvalue())
            with open(rp, encoding="utf-8") as f:
                self.assertTrue(any("fleet" in w
                                    for w in json.load(f)["warnings"]))

    def test_an_unwritable_history_is_a_warning_in_the_receipt(self):
        blocker = os.path.join(_tmp(), "not-a-dir")
        with open(blocker, "w", encoding="utf-8") as f:
            f.write("x")
        g = Guard(budget=5.0, quiet=True, anomaly=None)
        g.receipt_path = os.path.join(_tmp(), "r.json")
        g.history_path = os.path.join(blocker, "h.jsonl")
        with redirect_stderr(io.StringIO()), redirect_stdout(io.StringIO()):
            code = g.run(printer(result_line(cost=0.5)))
        self.assertEqual(code, 0)
        with open(g.receipt_path, encoding="utf-8") as f:
            rec = json.load(f)
        self.assertTrue(any("history" in w for w in rec["warnings"]),
                        rec["warnings"])


class TestNoBareInfinityOrNaN(unittest.TestCase):
    def test_write_receipt_turns_non_finite_numbers_into_null(self):
        path = os.path.join(_tmp(), "r.json")
        receipt_mod.write_receipt(
            {"cost_usd": float("inf"), "x": [float("nan"), 1.5],
             "nested": {"y": float("-inf")}}, path)
        with open(path, encoding="utf-8") as f:
            data = _strict_loads(f.read())
        self.assertEqual(data, {"cost_usd": None, "x": [None, 1.5],
                                "nested": {"y": None}})

    def test_a_non_finite_cost_is_n_a_in_the_receipt(self):
        rec = receipt_mod.build_receipt(
            command=["x"], started_at=0.0, ended_at=1.0, breached=False,
            breach_reason=None, cost_usd=float("inf"), cost_estimated=True,
            usage={}, num_turns=0, spawns=0, exit_code=0, model="m",
            session_id="s")
        self.assertIsNone(rec["cost_usd"])
        self.assertTrue(any("not a finite number" in w
                            for w in rec["warnings"]))
        text = receipt_mod.format_human(rec)
        self.assertIn("n/a (not a finite number)", text)
        self.assertNotIn("no usage data", text)

    def test_history_never_stores_or_trusts_a_non_finite_figure(self):
        path = os.path.join(_tmp(), "h.jsonl")
        base = {"agent": "a", "tokens": {"input": 10}, "breached": False,
                "ended_at": "t"}
        history_mod.record(dict(base, cost_usd=float("inf")), path)
        self.assertFalse(os.path.exists(path) and os.path.getsize(path) > 0,
                         "a run with an infinite cost must not be recorded")
        history_mod.record(dict(base, cost_usd=1.0), path)
        with open(path, "a", encoding="utf-8") as f:   # a legacy bad line
            f.write('{"agent": "a", "cost_usd": Infinity, "tokens": 5}' + chr(10))
        with open(path, encoding="utf-8") as f:
            for line in f.read().splitlines()[:1]:
                _strict_loads(line)
        warnings = []
        costs, tokens = history_mod._runs("a", "", path, warnings)
        self.assertEqual(costs, [1.0])
        self.assertTrue(any("corrupt" in w for w in warnings), warnings)

    def test_absurd_usage_is_a_clean_breach_not_a_traceback(self):
        huge = "1" + "0" * 400            # an integer far beyond any float
        line = ('{"type":"assistant","message":{"id":"m","content":[],'
                '"usage":{"input_tokens":' + huge + '}}}')
        g = Guard(budget=1.0, quiet=True, anomaly=None)
        code, rec, err = run_guard(g, printer(line))
        self.assertEqual(code, BREACH_EXIT_CODE)
        self.assertNotIn("Traceback", err)
        with open(g.receipt_path, encoding="utf-8") as f:
            _strict_loads(f.read())
        self.assertIsNone(rec["cost_usd"])

    def test_absurd_usage_without_a_cap_still_ends_cleanly(self):
        huge = "1" + "0" * 400
        line = ('{"type":"assistant","message":{"id":"m","content":[],'
                '"usage":{"input_tokens":' + huge + '}}}')
        g = Guard(max_spawns=5, quiet=True, anomaly=None)
        code, rec, err = run_guard(g, printer(line))
        self.assertEqual(code, 0)
        self.assertNotIn("Traceback", err)
        self.assertIsNone(rec["cost_usd"])
        with open(g.receipt_path, encoding="utf-8") as f:
            _strict_loads(f.read())
        if os.path.exists(g.history_path):
            with open(g.history_path, encoding="utf-8") as f:
                for l in f.read().splitlines():
                    _strict_loads(l)


# ---------------------------------------------------------------------
# L-8: the mutants that survived every earlier test. Each test below fails
# when the line named in its docstring is changed the way the mutant did.

def usage_event(mid, input_tokens=0, output_tokens=0, spawns=0):
    blocks = [{"type": "tool_use", "id": "%s-t%d" % (mid, i), "name": "Agent",
               "input": {}} for i in range(spawns)]
    return json.dumps({"type": "assistant", "message": {
        "id": mid, "content": blocks,
        "usage": {"input_tokens": input_tokens,
                  "output_tokens": output_tokens}}})


INIT_SONNET = json.dumps({"type": "system", "subtype": "init",
                          "model": "claude-sonnet-4-5", "session_id": "s"})


class TestTheCapComparisonsAreExact(unittest.TestCase):
    """Sonnet input costs $3 per 1M tokens, so 1,000,000 input tokens cost
    exactly $3 and every figure below is exact."""

    def _run(self, guard, *lines):
        return run_guard(guard, printer(INIT_SONNET, *lines))

    def test_budget_trips_at_one_and_a_half_times_the_cap(self):
        # guard.py _check_caps: `estimated_cost() > self.budget` (mutant: x2)
        g = Guard(budget=2.0, quiet=True, anomaly=None)
        code, rec, _ = self._run(g, usage_event("m", input_tokens=1000000))
        self.assertEqual(code, BREACH_EXIT_CODE)       # $3 > $2
        self.assertIs(rec["breach_live"], True)
        self.assertNotIn("after the fact", rec["breach_reason"])

    def test_budget_exactly_at_the_cap_and_one_token_over(self):
        g = Guard(budget=3.0, quiet=True, anomaly=None)
        code, rec, _ = self._run(g, usage_event("m", input_tokens=1000000))
        self.assertEqual(code, 0)                       # $3.00 is not > $3
        g = Guard(budget=3.0, quiet=True, anomaly=None)
        code, rec, _ = self._run(g, usage_event("m", input_tokens=1000001))
        self.assertEqual(code, BREACH_EXIT_CODE)        # $3.000003 > $3

    def test_token_cap_trips_at_one_and_a_half_times_the_cap(self):
        # guard.py _check_caps: `total_tokens() > self.max_tokens` (mutant: x2)
        g = Guard(max_tokens=1000, quiet=True, anomaly=None)
        code, rec, _ = self._run(g, usage_event("m", input_tokens=1500))
        self.assertEqual(code, BREACH_EXIT_CODE)
        self.assertIn("token cap exceeded", rec["breach_reason"])
        # stopped LIVE: the after-the-fact check at the end must not be what
        # caught it (it would mask a broken live comparison)
        self.assertNotIn("after the fact", rec["breach_reason"])
        self.assertIs(rec["breach_live"], True)

    def test_token_cap_exactly_at_the_cap_and_one_over(self):
        g = Guard(max_tokens=1000, quiet=True, anomaly=None)
        code, rec, _ = self._run(g, usage_event("m", input_tokens=600,
                                                output_tokens=400))
        self.assertEqual(code, 0)
        g = Guard(max_tokens=1000, quiet=True, anomaly=None)
        code, rec, _ = self._run(g, usage_event("m", input_tokens=600,
                                                output_tokens=401))
        self.assertEqual(code, BREACH_EXIT_CODE)
        self.assertIs(rec["breach_live"], True)

    def test_spawn_cap_trips_on_the_first_spawn_over_the_cap(self):
        # guard.py _check_caps: `spawns > self.max_spawns` (mutant: + 1)
        g = Guard(max_spawns=2, quiet=True, anomaly=None)
        code, rec, _ = self._run(g, usage_event("m", 10, 1, spawns=3))
        self.assertEqual(code, BREACH_EXIT_CODE)
        self.assertIn("spawn cap exceeded", rec["breach_reason"])
        self.assertEqual(rec["subagent_spawns"], 3)

    def test_spawn_cap_exactly_at_the_cap_is_fine(self):
        g = Guard(max_spawns=2, quiet=True, anomaly=None)
        code, rec, _ = self._run(g, usage_event("m", 10, 1, spawns=2))
        self.assertEqual(code, 0)
        self.assertFalse(rec["breached"])

    def test_a_re_emitted_block_is_one_spawn(self):
        # the same tool_use id twice (split events of one message) counts once
        g = Guard(max_spawns=1, quiet=True, anomaly=None)
        code, rec, _ = self._run(g, usage_event("m", 10, 1, spawns=1),
                                 usage_event("m", 10, 1, spawns=1))
        self.assertEqual(code, 0)
        self.assertEqual(rec["subagent_spawns"], 1)


class TestTelemetryCountsAFinalCostOnItsOwn(unittest.TestCase):
    def test_a_result_with_a_cost_and_no_usage_events_is_telemetry(self):
        # guard.py telemetry_seen: `... or self.reported_cost is not None`
        g = Guard(timeout=30.0, quiet=True, anomaly=None)
        code, rec, _ = run_guard(g, printer(result_line(cost=0.25)))
        self.assertTrue(g.telemetry_seen)
        self.assertTrue(rec["telemetry_seen"])
        self.assertEqual(rec["usage_events"], 0)
        self.assertEqual(rec["cost_usd"], 0.25)
        self.assertFalse(rec["cost_estimated"])
        self.assertNotIn("no usage data", receipt_mod.format_human(rec))

    def test_a_run_that_printed_nothing_useful_is_not(self):
        g = Guard(timeout=30.0, quiet=True, anomaly=None)
        code, rec, _ = run_guard(g, printer("hello", "{}", "[1, 2]"))
        self.assertFalse(g.telemetry_seen)
        self.assertIsNone(rec["cost_usd"])


SIGTERM_IGNORER = (
    "import os, signal, sys, time\n"
    "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
    "open(sys.argv[1], 'w', encoding='utf-8').write(str(os.getpid()))\n"
    "print('{\"type\": \"system\", \"subtype\": \"init\"}', flush=True)\n"
    "time.sleep(120)\n")

SIGTERM_TIDIER = (
    "import os, signal, sys, time\n"
    "def bye(*_):\n"
    "    open(sys.argv[2], 'w', encoding='utf-8').write('clean')\n"
    "    os._exit(0)\n"
    "signal.signal(signal.SIGTERM, bye)\n"
    "open(sys.argv[1], 'w', encoding='utf-8').write(str(os.getpid()))\n"
    "time.sleep(120)\n")


@unittest.skipUnless(POSIX, "SIGTERM / SIGKILL semantics are POSIX")
class TestTheGraceThenSigkillLadder(unittest.TestCase):
    def test_a_sigterm_ignorer_is_killed_after_the_grace_period(self):
        # proctree.py _kill_tree_posix: the final `_signal_group(SIGKILL)`
        tmp = _tmp()
        pidfile = os.path.join(tmp, "agent.pid")
        g = Guard(timeout=1.0, grace_period=0.5, quiet=True, anomaly=None)
        started = time.monotonic()
        code, rec, _ = run_guard(g, [PY, "-c", SIGTERM_IGNORER, pidfile])
        took = time.monotonic() - started
        agent = read_pid(pidfile)
        self.addCleanup(lambda: pid_alive(agent) and os.kill(agent, 9))
        self.assertEqual(code, BREACH_EXIT_CODE)
        self.assertIn("time limit exceeded", rec["breach_reason"])
        self.assertTrue(wait_dead(agent, 5), "SIGKILL never came")
        self.assertEqual(rec["child_returncode"], -signal.SIGKILL)
        self.assertLess(took, 15)

    def test_a_process_that_exits_on_sigterm_is_given_the_grace_period(self):
        # ... and is not SIGKILLed before it had the time to tidy up
        tmp = _tmp()
        pidfile = os.path.join(tmp, "agent.pid")
        marker = os.path.join(tmp, "tidied")
        g = Guard(timeout=1.0, grace_period=10.0, quiet=True, anomaly=None)
        code, rec, _ = run_guard(g, [PY, "-c", SIGTERM_TIDIER, pidfile, marker])
        agent = read_pid(pidfile)
        self.addCleanup(lambda: pid_alive(agent) and os.kill(agent, 9))
        self.assertEqual(code, BREACH_EXIT_CODE)
        self.assertTrue(os.path.exists(marker), "killed before it could exit")
        self.assertEqual(rec["child_returncode"], 0)

    def test_kill_tree_escalates_for_a_sigterm_ignorer(self):
        tmp = _tmp()
        pidfile = os.path.join(tmp, "agent.pid")
        proc = subprocess.Popen([PY, "-c", SIGTERM_IGNORER, pidfile],
                                stdout=subprocess.PIPE, text=True,
                                **proctree.popen_kwargs())
        try:
            self.assertIn("init", proc.stdout.readline())  # handler is set
            proctree.kill_tree(proc, grace=0.3)
            self.assertEqual(proc.wait(timeout=10), -signal.SIGKILL)
        finally:
            if proc.poll() is None:
                proc.kill()
            proc.stdout.close()
