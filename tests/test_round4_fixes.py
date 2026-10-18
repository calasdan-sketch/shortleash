"""Panel round 4: regression tests, one class per finding.

H-1 Ctrl-C / SIGTERM to the leash in the straggler window (the child had
    exited, a grandchild still held the stdout pipe, the leash was waiting
    up to two grace periods for it) found no SIGTERM handler any more
    (silent death) or Python's default Ctrl-C (traceback out of
    monitor.join): no receipt, stragglers left running.
M-1 `--receipt /nonexistent/r.json` was a FileNotFoundError traceback
    AFTER the run; a failing write at the end was one too.
L-1 `128 - returncode` for a negative returncode was not POSIX-guarded.
L-2 Comments claimed invalid JSON trips fail-closed; it is ignored (only a
    telemetry line with malformed fields trips).
L-3 `--anomaly` with fewer than --min-history runs was silent: nothing
    said the cap was not armed.
L-4 pyproject had no license / classifiers / URLs.
L-5 `shortleash cache guide | head -1`: BrokenPipeError traceback.
"""

import _isolate  # noqa: F401  (isolate ~/.shortleash before importing)

import io
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

from shortleash import cli as cli_mod
from shortleash import history as history_mod
from shortleash import proctree
from shortleash import receipt as receipt_mod
from shortleash.guard import (INTERRUPTED_EXIT_CODE, TERMINATED_EXIT_CODE,
                              Guard, agent_name, child_exit_code)

try:
    import tomllib  # Python 3.11+
except ModuleNotFoundError:  # 3.10: the metadata tests below skip themselves
    tomllib = None

PY = sys.executable
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
AGENT = agent_name(PY)
POSIX = not proctree.is_windows()

INIT_LINE = ("sys.stdout.write(json.dumps({'type': 'system', 'subtype': 'init', "
             "'model': 'claude-sonnet-4-5', 'session_id': 'x'}) + chr(10)); "
             "sys.stdout.flush(); ")
USAGE_LINE = ("sys.stdout.write(json.dumps({'type': 'assistant', 'message': "
              "{'id': 'm1', 'content': [], 'usage': {'input_tokens': 100, "
              "'output_tokens': 10}}}) + chr(10)); sys.stdout.flush(); ")
RESULT_LINE = ("sys.stdout.write(json.dumps({'type': 'result', 'subtype': "
               "'success', 'is_error': False, 'total_cost_usd': 0.01, "
               "'num_turns': 1}) + chr(10)); sys.stdout.flush(); ")

# Child: telemetry, then spawn a grandchild (same process group, inherits
# our stdout pipe) that writes its pid to argv[1] and sleeps; then exit.
ESCAPER_CHILD = (
    "import json, subprocess, sys; " + INIT_LINE + USAGE_LINE +
    "subprocess.Popen([sys.executable, '-c', \"import os, sys, time; "
    "open(sys.argv[1], 'w', encoding='utf-8').write(str(os.getpid())); "
    "time.sleep(60)\", sys.argv[1]]); " + RESULT_LINE)

# Child: a plain-text line and a JSON array between valid telemetry.
CHATTY_CHILD = (
    "import json, sys; " + INIT_LINE +
    "sys.stdout.write('warning: entrypoint chatter' + chr(10)); "
    "sys.stdout.write('[1, 2, 3]' + chr(10)); sys.stdout.flush(); "
    + USAGE_LINE + RESULT_LINE)

QUIET_CHILD = "import json, sys; " + INIT_LINE + USAGE_LINE + RESULT_LINE


def _tmp() -> str:
    return tempfile.mkdtemp(prefix="sl4-")


def _pid_alive(pid: int) -> bool:
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


def _wait_dead(pid: int, seconds: float) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if not _pid_alive(pid):
            return True
        time.sleep(0.05)
    return not _pid_alive(pid)


def _read_pid(path: str, seconds: float = 15.0) -> int:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        try:
            with open(path, encoding="utf-8") as f:
                txt = f.read().strip()
            if txt:
                return int(txt)
        except OSError:
            pass
        time.sleep(0.05)
    raise AssertionError("grandchild never wrote its pid")


def _leash(*args: str, stdout=subprocess.DEVNULL, stderr=None):
    """The leash as a real subprocess, in its own session so a signal
    aimed at it never reaches this test process."""
    return subprocess.Popen([PY, "-m", "shortleash", "run", *args],
                            cwd=REPO, stdout=stdout, stderr=stderr,
                            start_new_session=True)


def _run_guard(guard: Guard, argv: list[str]) -> tuple[int, dict | None, str]:
    tmp = _tmp()
    if guard.receipt_path is None:
        guard.receipt_path = os.path.join(tmp, "r.json")
    guard.history_path = guard.history_path or os.path.join(tmp, "h.jsonl")
    err = io.StringIO()
    with redirect_stderr(err), redirect_stdout(io.StringIO()):
        code = guard.run(argv)
    rec = None
    if os.path.isfile(guard.receipt_path):
        with open(guard.receipt_path, encoding="utf-8") as f:
            rec = json.load(f)
    return code, rec, err.getvalue()


# ---------------------------------------------------------------------
# H-1

@unittest.skipUnless(POSIX, "os.kill with SIGINT/SIGTERM: POSIX only")
class TestSignalInStragglerWindow(unittest.TestCase):
    def _signal_in_window(self, sig) -> tuple[int, dict, str]:
        tmp = _tmp()
        pidfile = os.path.join(tmp, "grandchild.pid")
        rp = os.path.join(tmp, "r.json")
        errpath = os.path.join(tmp, "err.txt")
        with open(errpath, "w", encoding="utf-8") as errf:
            p = _leash("--budget", "50", "--grace-period", "4",
                       "--history-path", os.path.join(tmp, "h.jsonl"),
                       "--receipt", rp, "--", PY, "-c", ESCAPER_CHILD,
                       pidfile, stderr=errf)
            gpid = _read_pid(pidfile)
            # The child exits right after spawning; the leash notices
            # within one 0.5 s poll and starts waiting for the pipe (up
            # to 4 s). Signal it inside that window.
            time.sleep(1.2)
            os.kill(p.pid, sig)
            try:
                rc = p.wait(timeout=30)
            except subprocess.TimeoutExpired:
                os.killpg(p.pid, signal.SIGKILL)
                self.fail("leash did not return after the signal")
        try:
            self.assertTrue(_wait_dead(gpid, 10),
                            "the grandchild kept running after the leash was "
                            "signalled")
        finally:
            if _pid_alive(gpid):
                os.kill(gpid, signal.SIGKILL)
        with open(errpath, encoding="utf-8", errors="replace") as f:
            err = f.read()
        self.assertNotIn("Traceback", err)
        self.assertTrue(os.path.exists(rp), f"no receipt written; stderr: {err}")
        with open(rp, encoding="utf-8") as f:
            rec = json.load(f)
        self.assertTrue(rec["breached"])
        self.assertIn("after the command exited", rec["breach_reason"])
        self.assertTrue(any("left child process(es) running" in w
                            for w in rec["warnings"]), rec["warnings"])
        self.assertIn("STOPPED:", err)
        return rc, rec, err

    def test_ctrl_c_in_straggler_window(self):
        rc, rec, _ = self._signal_in_window(signal.SIGINT)
        self.assertEqual(rc, INTERRUPTED_EXIT_CODE)
        self.assertEqual(rec["exit_code"], INTERRUPTED_EXIT_CODE)
        self.assertIn("interrupted (Ctrl-C)", rec["breach_reason"])

    def test_sigterm_in_straggler_window(self):
        rc, rec, _ = self._signal_in_window(signal.SIGTERM)
        self.assertEqual(rc, TERMINATED_EXIT_CODE)
        self.assertEqual(rec["exit_code"], TERMINATED_EXIT_CODE)
        self.assertIn("terminated (SIGTERM)", rec["breach_reason"])

    def test_handlers_stay_until_the_receipt_is_written(self):
        # In-process: a KeyboardInterrupt raised while the leash waits for
        # the straggler must be handled, not propagate.
        tmp = _tmp()
        pidfile = os.path.join(tmp, "grandchild.pid")
        real_join = Guard._join_monitor
        fired = {"n": 0}

        def interrupting_join(self_guard, monitor, timeout):
            if fired["n"] == 0 and self_guard.proc.poll() is not None:
                fired["n"] += 1
                _read_pid(pidfile)  # the straggler is up and holds the pipe
                raise KeyboardInterrupt()
            return real_join(self_guard, monitor, timeout)

        g = Guard(budget=50.0, grace_period=4.0, quiet=True)
        with mock.patch.object(Guard, "_join_monitor", interrupting_join):
            code, rec, _ = _run_guard(g, [PY, "-c", ESCAPER_CHILD, pidfile])
        self.assertEqual(fired["n"], 1, "the fake Ctrl-C never fired")
        self.assertEqual(code, INTERRUPTED_EXIT_CODE)
        self.assertIsNotNone(rec)
        self.assertIn("after the command exited", rec["breach_reason"])
        gpid = _read_pid(pidfile)
        self.assertTrue(_wait_dead(gpid, 10), "grandchild survived")
        self.assertEqual(signal.getsignal(signal.SIGINT),
                         signal.default_int_handler)  # restored afterwards


# ---------------------------------------------------------------------
# M-1

class TestReceiptPathProblems(unittest.TestCase):
    def test_missing_directory_is_created_before_launch(self):
        tmp = _tmp()
        rp = os.path.join(tmp, "not", "yet", "r.json")
        g = Guard(quiet=True, receipt_path=rp)
        code, rec, err = _run_guard(g, [PY, "-c", QUIET_CHILD])
        self.assertEqual(code, 0, err)
        self.assertIsNotNone(rec, "no receipt written")
        self.assertEqual(rec["exit_code"], 0)
        self.assertNotIn("Traceback", err)

    def test_impossible_receipt_path_is_exit_2_before_launch(self):
        tmp = _tmp()
        blocker = os.path.join(tmp, "a-file")
        with open(blocker, "w", encoding="utf-8") as f:
            f.write("x")
        marker = os.path.join(tmp, "child-ran")
        g = Guard(quiet=True, receipt_path=os.path.join(blocker, "r.json"))
        err = io.StringIO()
        with redirect_stderr(err), redirect_stdout(io.StringIO()):
            code = g.run([PY, "-c",
                          f"open({marker!r}, 'w').write('ran')"])
        self.assertEqual(code, 2)
        self.assertIn("cannot write the receipt", err.getvalue())
        self.assertNotIn("Traceback", err.getvalue())
        self.assertFalse(os.path.exists(marker), "the child was launched")
        self.assertIsNone(g.proc)

    def test_prepare_leaves_an_existing_file_alone(self):
        tmp = _tmp()
        rp = os.path.join(tmp, "r.json")
        with open(rp, "w", encoding="utf-8") as f:
            f.write("old receipt")
        receipt_mod.prepare_receipt_path(rp)
        with open(rp, encoding="utf-8") as f:
            self.assertEqual(f.read(), "old receipt")
        fresh = os.path.join(tmp, "new", "r.json")
        receipt_mod.prepare_receipt_path(fresh)
        self.assertTrue(os.path.isdir(os.path.dirname(fresh)))
        self.assertFalse(os.path.exists(fresh))  # the probe file is gone

    def test_write_failure_after_the_run_is_a_warning_not_a_traceback(self):
        g = Guard(quiet=True)

        def boom(rec, path):
            raise OSError(28, "No space left on device")

        with mock.patch.object(receipt_mod, "write_receipt", boom):
            code, rec, err = _run_guard(g, [PY, "-c", QUIET_CHILD])
        self.assertEqual(code, 0)
        self.assertIsNone(rec)  # nothing could be written...
        self.assertIn("receipt could not be written", err)
        self.assertIn("No space left on device", err)
        # ...so the summary is printed even under --quiet
        self.assertIn("shortleash receipt", err)
        self.assertIn("warning:   receipt could not be written", err)
        self.assertNotIn("Traceback", err)


# ---------------------------------------------------------------------
# L-1

class TestChildExitCodeMapping(unittest.TestCase):
    def test_posix_signal_death_is_128_plus_n(self):
        with mock.patch.object(proctree.os, "name", "posix"), \
                mock.patch.object(proctree.sys, "platform", "linux"):
            self.assertEqual(child_exit_code(-9), 137)
            self.assertEqual(child_exit_code(-15), 143)
            self.assertEqual(child_exit_code(0), 0)
            self.assertEqual(child_exit_code(7), 7)

    def test_windows_status_is_passed_through_clamped(self):
        with mock.patch.object(proctree.os, "name", "nt"), \
                mock.patch.object(proctree.sys, "platform", "win32"):
            self.assertTrue(proctree.is_windows())
            # 0xC0000005 (access violation) as Popen hands it over, and
            # as a signed 32-bit view: never "128 - n" nonsense.
            self.assertEqual(child_exit_code(3221225477), 255)
            self.assertEqual(child_exit_code(-1073741819), 255)
            self.assertEqual(child_exit_code(1), 1)
            self.assertEqual(child_exit_code(255), 255)
            self.assertEqual(child_exit_code(0), 0)

    def test_receipt_keeps_the_raw_returncode(self):
        g = Guard(quiet=True)
        code, rec, _ = _run_guard(g, [PY, "-c", "import sys; sys.exit(7)"])
        self.assertEqual(code, 7)
        self.assertEqual(rec["exit_code"], 7)
        self.assertEqual(rec["child_returncode"], 7)

    @unittest.skipUnless(POSIX, "signal death: POSIX only")
    def test_receipt_raw_returncode_for_signal_death(self):
        g = Guard(quiet=True)
        code, rec, _ = _run_guard(
            g, [PY, "-c", "import os, signal; os.kill(os.getpid(), signal.SIGKILL)"])
        self.assertEqual(code, 137)
        self.assertEqual(rec["exit_code"], 137)
        self.assertEqual(rec["child_returncode"], -9)


# ---------------------------------------------------------------------
# L-2

class TestNonJsonLinesAreIgnored(unittest.TestCase):
    def test_chatter_does_not_trip(self):
        g = Guard(budget=50.0, quiet=True)
        code, rec, _ = _run_guard(g, [PY, "-c", CHATTY_CHILD])
        self.assertEqual(code, 0)
        self.assertFalse(rec["breached"])
        self.assertEqual(rec["tokens"]["input"], 100)

    def test_comments_do_not_claim_bad_json_trips(self):
        src = os.path.join(REPO, "shortleash", "guard.py")
        with open(src, encoding="utf-8") as f:
            text = f.read()
        self.assertNotIn("(bad JSON, or", text)
        self.assertIn("ignored", text.split("def _monitor_stdout")[1]
                      .split("def _handle_obs")[0])
        with open(os.path.join(REPO, "README.md"), encoding="utf-8") as f:
            readme = f.read()
        self.assertIn("Non-JSON lines", readme)


# ---------------------------------------------------------------------
# L-3

def _seed_history(path: str, agent: str, costs, tokens: int = 2000) -> None:
    for c in costs:
        history_mod.record({"agent": agent, "profile": "", "cost_usd": c,
                            "tokens": {"input": tokens, "output": 0,
                                       "cache_write": 0, "cache_read": 0},
                            "breached": False, "ended_at": ""}, path)


class TestAnomalyNotArmed(unittest.TestCase):
    def test_receipt_says_not_armed_and_enforced_does_not_imply_it(self):
        tmp = _tmp()
        hp = os.path.join(tmp, "h.jsonl")
        _seed_history(hp, AGENT, [0.01, 0.02])
        g = Guard(budget=50.0, anomaly=3.0, min_history=5, quiet=True,
                  history_path=hp)
        code, rec, err = _run_guard(g, [PY, "-c", QUIET_CHILD])
        self.assertEqual(code, 0)
        self.assertEqual(rec["anomaly"], {"armed": False, "runs": 2,
                                          "min_runs": 5, "multiplier": 3.0})
        self.assertNotIn("anomaly", rec["enforced"])
        self.assertNotIn("anomaly", rec["not_enforced"])
        self.assertIn("budget", rec["enforced"])
        self.assertTrue(any(w.startswith("anomaly mode not armed (2 of 5")
                            for w in rec["warnings"]), rec["warnings"])
        human = receipt_mod.format_human(rec)
        self.assertIn("anomaly:   not armed (2 of 5 runs)", human)
        self.assertEqual(history_mod.count_runs(AGENT, path=hp), 3)  # recorded

    def test_armed_once_enough_runs(self):
        tmp = _tmp()
        hp = os.path.join(tmp, "h.jsonl")
        _seed_history(hp, AGENT, [0.01] * 5)
        g = Guard(anomaly=3.0, min_history=5, quiet=True, history_path=hp)
        code, rec, _ = _run_guard(g, [PY, "-c", QUIET_CHILD])
        self.assertEqual(code, 0)
        self.assertEqual(rec["anomaly"]["armed"], True)
        self.assertEqual(rec["anomaly"]["runs"], 5)
        self.assertIn("anomaly", rec["enforced"])
        self.assertNotIn("not armed", receipt_mod.format_human(rec))

    def test_not_requested_is_none(self):
        g = Guard(quiet=True)
        _, rec, _ = _run_guard(g, [PY, "-c", QUIET_CHILD])
        self.assertIsNone(rec["anomaly"])


# ---------------------------------------------------------------------
# L-4

class TestPackagingMetadata(unittest.TestCase):
    @unittest.skipIf(tomllib is None, "tomllib is Python 3.11+; "
                     "pyproject.toml is the same file on every interpreter")
    def test_pyproject_license_classifiers_urls(self):
        with open(os.path.join(REPO, "pyproject.toml"), "rb") as f:
            data = tomllib.load(f)
        proj = data["project"]
        self.assertEqual(proj["license"], "MIT")
        self.assertEqual(proj["license-files"], ["LICENSE"])
        self.assertTrue(os.path.isfile(os.path.join(REPO, "LICENSE")))
        self.assertIn("Programming Language :: Python :: 3.11",
                      proj["classifiers"])
        self.assertIn("Operating System :: OS Independent", proj["classifiers"])
        self.assertFalse(any(c.startswith("License ::")
                             for c in proj["classifiers"]))  # SPDX instead
        self.assertIn("Homepage", proj["urls"])
        requires = data["build-system"]["requires"]
        self.assertTrue(any(r.startswith("setuptools>=77") for r in requires),
                        requires)


# ---------------------------------------------------------------------
# L-5

class TestBrokenPipe(unittest.TestCase):
    def test_cache_guide_piped_to_a_closed_reader(self):
        p = subprocess.Popen([PY, "-m", "shortleash", "cache", "guide"],
                             cwd=REPO, stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE)
        p.stdout.close()
        err = p.stderr.read()
        p.stderr.close()
        self.assertEqual(p.wait(timeout=60), 0, err)
        self.assertNotIn(b"Traceback", err)
        self.assertNotIn(b"BrokenPipe", err)

    def test_in_process_broken_stdout(self):
        class Gone(io.StringIO):
            def write(self, s):
                raise BrokenPipeError(32, "Broken pipe")
        with redirect_stdout(Gone()), redirect_stderr(io.StringIO()):
            self.assertEqual(cli_mod.main(["cache", "guide"]), 0)


if __name__ == "__main__":
    unittest.main()
