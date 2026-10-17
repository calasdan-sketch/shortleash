"""Panel round 3 (second pass): regression tests, one class per finding.

L-1 Ctrl-C / SIGTERM to the leash used to kill only the leash (traceback,
    rc 130/-15, no receipt) and leave the agent running as an orphan in
    its own process group. The leash must kill the tree, write the
    receipt and exit 130 / 143.
L-2 The echo of the child's output to our stdout sat outside the try:
    when stdout closed (`| head -1`) the monitor thread died, the child
    blocked on a full pipe and the leash hung with no caps checked.
L-3 Anomaly mode was on by default and breached runs never enter the
    baseline, so after five small runs every larger legitimate task was
    killed on EVERY attempt (and masked a --max-spawns cap). It is opt-in
    now (--anomaly), and the STOPPED line says how to leave the ratchet.
L-4 A corrupt history line ("cost_usd": "abc") was a ValueError traceback
    before the agent even started.
L-5 Claude Code emits one `assistant` event per content block, all with
    the same message.id and the same usage -> double counting. The
    subagent tool is named `Agent` now (`Task` in older versions).
L-6 Windows: launching the npm `claude.cmd` shim runs through cmd.exe,
    which expands/mangles prompt text (`%VAR%`, `^`, `"`...). The shim is
    resolved to `node cli.js`; other .cmd/.bat targets refuse unsafe args.
L-7 Banner printed `budget=$None ...`; `--timeout 0` tripped instantly;
    a child killed by SIGKILL outside a breach returned -9 (247 to a shell).
L-8 events.py claimed terminal success is gated on is_error but never
    read it; README had `git clone <repo>` and a bash alias.
L-9 The prompt was stored verbatim in the receipt (`-p "my password is
    hunter2"`): --redact-prompt.
Simplify: fleet list, load_pricing removed, no dict-compat shim.
"""

import _isolate  # noqa: F401  (isolate ~/.shortleash before importing)

import io
import json
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
import types
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

from shortleash import cli as cli_mod
from shortleash import events
from shortleash import fleet as fleet_mod
from shortleash import history as history_mod
from shortleash import launch
from shortleash import pricing
from shortleash import proctree
from shortleash import receipt as receipt_mod
from shortleash.guard import (AGENT_ERROR_EXIT_CODE, BREACH_EXIT_CODE,
                              INTERRUPTED_EXIT_CODE, TERMINATED_EXIT_CODE,
                              Guard, agent_name)

PY = sys.executable
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SIM = os.path.join(REPO, "sim", "runaway.py")
AGENT = agent_name(PY)
POSIX = not proctree.is_windows()

INIT_LINE = ("sys.stdout.write(json.dumps({'type': 'system', 'subtype': 'init', "
             "'model': 'claude-sonnet-4-5', 'session_id': 'x'}) + chr(10)); "
             "sys.stdout.flush(); ")

# Child: write own pid to argv[1], emit an init line, then sleep 60s.
SLEEPING_CHILD = (
    "import os, sys, time, json; "
    "open(sys.argv[1], 'w', encoding='utf-8').write(str(os.getpid())); "
    + INIT_LINE + "time.sleep(60)")

# Child: init line, half a second of silence, then one huge usage line
# (so a closed stdout is noticed on the echo of the line that breaches).
LATE_RUNAWAY_CHILD = (
    "import sys, time, json; " + INIT_LINE + "time.sleep(0.5); "
    "sys.stdout.write(json.dumps({'type': 'assistant', 'message': {'id': 'm1', "
    "'content': [], 'usage': {'input_tokens': 5000000, 'output_tokens': 1}}}) "
    "+ chr(10)); sys.stdout.flush(); time.sleep(30)")


def _tmp() -> str:
    return tempfile.mkdtemp(prefix="sl3-")


def _pid_alive(pid: int) -> bool:
    if os.name == "nt":  # os.kill(pid, 0) is not a probe on Windows
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


def _read_pid(path: str, seconds: float = 10.0) -> int:
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
    raise AssertionError("child never wrote its pid")


def _leash(*args: str, stdout=subprocess.DEVNULL, stderr=None):
    """The leash as a real subprocess, in its own session so a signal
    aimed at it never reaches this test process."""
    return subprocess.Popen([PY, "-m", "shortleash", "run", *args],
                            cwd=REPO, stdout=stdout, stderr=stderr,
                            start_new_session=True)


def _run_guard(guard: Guard, argv: list[str]) -> tuple[int, dict, str]:
    tmp = _tmp()
    guard.receipt_path = os.path.join(tmp, "r.json")
    guard.history_path = os.path.join(tmp, "h.jsonl")
    err = io.StringIO()
    with redirect_stderr(err), redirect_stdout(io.StringIO()):
        code = guard.run(argv)
    with open(guard.receipt_path, encoding="utf-8") as f:
        rec = json.load(f)
    return code, rec, err.getvalue()


# ---------------------------------------------------------------------
# L-1

@unittest.skipUnless(POSIX, "os.kill with SIGINT/SIGTERM: POSIX only")
class TestSignalsToTheLeash(unittest.TestCase):
    def _signal_leash(self, sig) -> tuple[int, dict, str, int]:
        tmp = _tmp()
        pidfile = os.path.join(tmp, "child.pid")
        rp = os.path.join(tmp, "r.json")
        errpath = os.path.join(tmp, "err.txt")
        with open(errpath, "w", encoding="utf-8") as errf:
            p = _leash("--budget", "5", "--grace-period", "1",
                       "--history-path", os.path.join(tmp, "h.jsonl"),
                       "--receipt", rp, "--", PY, "-c", SLEEPING_CHILD,
                       pidfile, stderr=errf)
            cpid = _read_pid(pidfile)
            time.sleep(0.3)  # let the leash reach its wait loop
            os.kill(p.pid, sig)
            try:
                rc = p.wait(timeout=30)
            except subprocess.TimeoutExpired:
                os.killpg(p.pid, signal.SIGKILL)
                self.fail("leash did not return after the signal")
        try:
            self.assertTrue(_wait_dead(cpid, 10),
                            "the agent kept running after the leash was signalled")
        finally:
            if _pid_alive(cpid):
                os.kill(cpid, signal.SIGKILL)
        self.assertTrue(os.path.exists(rp), "no receipt written")
        with open(rp, encoding="utf-8") as f:
            rec = json.load(f)
        with open(errpath, encoding="utf-8") as f:
            err = f.read()
        self.assertNotIn("Traceback", err)
        return rc, rec, err, cpid

    def test_ctrl_c_kills_tree_writes_receipt_exit_130(self):
        rc, rec, err, _ = self._signal_leash(signal.SIGINT)
        self.assertEqual(rc, INTERRUPTED_EXIT_CODE)
        self.assertTrue(rec["breached"])
        self.assertIn("interrupted (Ctrl-C)", rec["breach_reason"])
        self.assertEqual(rec["exit_code"], INTERRUPTED_EXIT_CODE)
        self.assertIn("STOPPED:", err)

    def test_sigterm_kills_tree_writes_receipt_exit_143(self):
        rc, rec, err, _ = self._signal_leash(signal.SIGTERM)
        self.assertEqual(rc, TERMINATED_EXIT_CODE)
        self.assertTrue(rec["breached"])
        self.assertIn("terminated (SIGTERM)", rec["breach_reason"])
        self.assertEqual(rec["exit_code"], TERMINATED_EXIT_CODE)

    def test_child_killed_by_signal_maps_to_128_plus_n(self):
        # L-7: a child that dies of SIGKILL outside any breach used to make
        # the leash return -9 (247 to a shell); shells say 137.
        g = Guard(quiet=True)
        code, rec, _ = _run_guard(
            g, [PY, "-c", "import os, signal; os.kill(os.getpid(), signal.SIGKILL)"])
        self.assertEqual(code, 137)
        self.assertEqual(rec["exit_code"], 137)
        self.assertFalse(rec["breached"])


class TestKeyboardInterruptInProcess(unittest.TestCase):
    """Cross-platform: a KeyboardInterrupt surfacing from the wait must be
    handled like Ctrl-C (tree killed, receipt, 130) -- never propagate."""

    def test_keyboard_interrupt_during_wait(self):
        tmp = _tmp()
        pidfile = os.path.join(tmp, "child.pid")
        real_wait = subprocess.Popen.wait
        fired = {"n": 0}

        def interrupting_wait(self_proc, timeout=None):
            if fired["n"] == 0 and os.path.exists(pidfile):
                fired["n"] += 1
                raise KeyboardInterrupt()
            return real_wait(self_proc, timeout=timeout)

        g = Guard(budget=5.0, grace_period=1.0, quiet=True)
        with mock.patch.object(subprocess.Popen, "wait", interrupting_wait):
            code, rec, _ = _run_guard(g, [PY, "-c", SLEEPING_CHILD, pidfile])
        self.assertEqual(fired["n"], 1, "the fake Ctrl-C never fired")
        self.assertEqual(code, INTERRUPTED_EXIT_CODE)
        self.assertTrue(rec["breached"])
        self.assertIn("interrupted (Ctrl-C)", rec["breach_reason"])
        cpid = _read_pid(pidfile)
        self.assertTrue(_wait_dead(cpid, 10), "agent survived the Ctrl-C")

    def test_wait_loop_polls_with_timeout(self):
        # Windows: an indefinite WaitForSingleObject cannot be interrupted
        # by Ctrl-C, so the leash must wait in short slices.
        seen = []
        real_wait = subprocess.Popen.wait

        def spy(self_proc, timeout=None):
            seen.append(timeout)
            return real_wait(self_proc, timeout=timeout)

        g = Guard(quiet=True)
        with mock.patch.object(subprocess.Popen, "wait", spy):
            _run_guard(g, [PY, "-c", "import time; time.sleep(1.2)"])
        self.assertTrue(seen, "wait() never called")
        self.assertTrue(all(t is not None and t <= 1.0 for t in seen), seen)
        self.assertGreater(len(seen), 1)


# ---------------------------------------------------------------------
# L-2

class _BrokenStdout:
    """A stdout whose consumer went away (`| head -1`). Deliberately not an
    io.TextIOBase: its __del__ would flush (and raise) at teardown."""

    def write(self, s):
        raise BrokenPipeError(32, "Broken pipe")

    def flush(self):
        raise BrokenPipeError(32, "Broken pipe")


class TestClosedStdout(unittest.TestCase):
    def test_echo_failure_keeps_caps_working_in_process(self):
        g = Guard(budget=0.05, grace_period=0.5, quiet=True)
        tmp = _tmp()
        g.receipt_path = os.path.join(tmp, "r.json")
        g.history_path = os.path.join(tmp, "h.jsonl")
        with redirect_stdout(_BrokenStdout()), redirect_stderr(io.StringIO()):
            code = g.run([PY, SIM, "--turns", "400", "--tokens-per-turn",
                          "15000", "--delay", "0.01"])
        self.assertEqual(code, BREACH_EXIT_CODE)
        self.assertFalse(g.echo)
        with open(g.receipt_path, encoding="utf-8") as f:
            rec = json.load(f)
        self.assertIn("budget exceeded", rec["breach_reason"])
        self.assertTrue(any("stdout closed" in w for w in rec["warnings"]),
                        rec["warnings"])

    def test_pipe_closed_by_consumer_end_to_end(self):
        tmp = _tmp()
        rp = os.path.join(tmp, "r.json")
        errpath = os.path.join(tmp, "err.txt")
        with open(errpath, "w", encoding="utf-8") as errf:
            p = _leash("--budget", "1", "--grace-period", "0.5",
                       "--history-path", os.path.join(tmp, "h.jsonl"),
                       "--receipt", rp, "--", PY, "-c", LATE_RUNAWAY_CHILD,
                       stdout=subprocess.PIPE, stderr=errf)
            first = p.stdout.readline()          # like `head -1`...
            p.stdout.close()                     # ...then the reader exits
            try:
                rc = p.wait(timeout=30)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(p.pid, signal.SIGKILL)
                except (AttributeError, OSError):
                    p.kill()
                self.fail("leash hung after its stdout was closed")
        self.assertIn(b'"init"', first)
        self.assertEqual(rc, BREACH_EXIT_CODE)
        with open(rp, encoding="utf-8") as f:
            rec = json.load(f)
        self.assertIn("budget exceeded", rec["breach_reason"])
        self.assertTrue(any("stdout closed" in w for w in rec["warnings"]),
                        rec["warnings"])
        # the leash's stderr is in the console encoding (cp1252 on Windows)
        with open(errpath, encoding="utf-8", errors="replace") as f:
            err = f.read()
        self.assertNotIn("Traceback", err)
        self.assertNotIn("Exception ignored", err)


# ---------------------------------------------------------------------
# L-3

def _seed_history(path: str, agent: str, costs, tokens: int = 2000) -> None:
    for c in costs:
        history_mod.record({"agent": agent, "profile": "", "cost_usd": c,
                            "tokens": {"input": tokens, "output": 0,
                                       "cache_write": 0, "cache_read": 0},
                            "breached": False, "ended_at": ""}, path)


class TestAnomalyOptIn(unittest.TestCase):
    def _cli(self, *args) -> tuple[int, dict, str]:
        tmp = _tmp()
        rp = os.path.join(tmp, "r.json")
        err = io.StringIO()
        with redirect_stderr(err), redirect_stdout(io.StringIO()):
            code = cli_mod.main(["run", "--receipt", rp, *args])
        with open(rp, encoding="utf-8") as f:
            rec = json.load(f)
        return code, rec, err.getvalue()

    def test_parser_forms(self):
        parser = cli_mod._build_parser()
        self.assertIsNone(parser.parse_args(["run", "--", "x"]).anomaly)
        self.assertEqual(parser.parse_args(["run", "--anomaly", "--", "x"]).anomaly,
                         cli_mod.DEFAULT_ANOMALY)
        self.assertEqual(parser.parse_args(["run", "--anomaly", "5", "--", "x"]).anomaly,
                         5.0)
        self.assertIsNone(Guard().anomaly, "Guard must default to anomaly off")

    def test_off_by_default_static_cap_decides(self):
        # Five cheap runs on file, then a bigger task under --max-spawns 2:
        # the spawn cap must be what trips, not an anomaly verdict.
        tmp = _tmp()
        hist = os.path.join(tmp, "h.jsonl")
        _seed_history(hist, AGENT, [0.010, 0.012, 0.009, 0.011, 0.013])
        code, rec, err = self._cli(
            "--max-spawns", "2", "--history-path", hist, "--grace-period", "0.5",
            "--", PY, SIM, "--turns", "20", "--tokens-per-turn", "15000",
            "--delay", "0.02")
        self.assertEqual(code, BREACH_EXIT_CODE)
        self.assertIn("spawn cap exceeded", rec["breach_reason"])
        self.assertIsNone(rec["baseline"])
        self.assertNotIn("anomaly", err.split("pricing:")[0])

    def test_bigger_legit_task_is_not_killed_by_default(self):
        tmp = _tmp()
        hist = os.path.join(tmp, "h.jsonl")
        _seed_history(hist, AGENT, [0.010, 0.012, 0.009, 0.011, 0.013])
        code, rec, _ = self._cli(
            "--history-path", hist, "--", PY, SIM, "--turns", "6",
            "--tokens-per-turn", "15000", "--spawns", "0", "--delay", "0.01")
        self.assertEqual(code, 0)
        self.assertFalse(rec["breached"])
        # ...and it now enters the baseline for later --anomaly runs
        b = history_mod.baseline(AGENT, path=hist, min_runs=6)
        self.assertEqual(b["runs"], 6)

    def test_opt_in_trips_and_hints_the_way_out(self):
        tmp = _tmp()
        hist = os.path.join(tmp, "h.jsonl")
        _seed_history(hist, AGENT, [0.010, 0.012, 0.009, 0.011, 0.013])
        code, rec, err = self._cli(
            "--anomaly", "--history-path", hist, "--grace-period", "0.5",
            "--", PY, SIM, "--turns", "20", "--tokens-per-turn", "15000",
            "--spawns", "0", "--delay", "0.02")
        self.assertEqual(code, BREACH_EXIT_CODE)
        self.assertIn("anomaly", rec["breach_reason"])
        self.assertIn("--anomaly", rec["hint"])
        self.assertIn("--profile", rec["hint"])
        self.assertIn("hint:", err)
        self.assertIn("anomaly=3x", err)

    def test_no_anomaly_flag_still_accepted(self):
        tmp = _tmp()
        code, rec, _ = self._cli("--no-anomaly", "--history-path",
                                 os.path.join(tmp, "h"), "--", PY, SIM,
                                 "--well-behaved")
        self.assertEqual(code, 0)


# ---------------------------------------------------------------------
# L-4

class TestCorruptHistory(unittest.TestCase):
    def test_corrupt_lines_skipped_with_warning(self):
        tmp = _tmp()
        hist = os.path.join(tmp, "h.jsonl")
        _seed_history(hist, "claude", [0.1, 0.2, 0.3, 0.4, 0.5])
        with open(hist, "a", encoding="utf-8") as f:
            f.write(json.dumps({"agent": "claude", "cost_usd": "abc"}) + "\n")
            f.write(json.dumps({"agent": "claude", "cost_usd": 1, "tokens": [1]}) + "\n")
            f.write("42\n")
            f.write("{not json\n")
        warnings: list[str] = []
        b = history_mod.baseline("claude", path=hist, warnings=warnings)
        self.assertEqual(b["runs"], 5)
        self.assertAlmostEqual(b["median_cost"], 0.3)
        self.assertEqual(len(warnings), 1)
        self.assertIn("skipped 4 corrupt line(s)", warnings[0])

    def test_guard_starts_despite_corrupt_history(self):
        tmp = _tmp()
        hist = os.path.join(tmp, "h.jsonl")
        with open(hist, "w", encoding="utf-8") as f:
            f.write(json.dumps({"agent": AGENT, "cost_usd": "abc"}) + "\n")
        g = Guard(anomaly=3.0, quiet=True, history_path=hist)
        g.receipt_path = os.path.join(tmp, "r.json")
        with redirect_stderr(io.StringIO()), redirect_stdout(io.StringIO()):
            code = g.run([PY, SIM, "--well-behaved"])
        self.assertEqual(code, 0)
        self.assertTrue(any("corrupt" in w for w in g.warnings), g.warnings)
        with open(g.receipt_path, encoding="utf-8") as f:
            rec = json.load(f)
        self.assertTrue(any("corrupt" in w for w in rec["warnings"]))


# ---------------------------------------------------------------------
# L-5

def _assistant(mid, usage, blocks=()):
    return json.dumps({"type": "assistant",
                       "message": {"id": mid, "content": list(blocks),
                                   "usage": usage}}) + "\n"


def _feed(guard: Guard, text: str) -> None:
    guard.proc = types.SimpleNamespace(stdout=io.StringIO(text), poll=lambda: 0)
    with redirect_stdout(io.StringIO()):
        guard._monitor_stdout()


class TestUsageDedupe(unittest.TestCase):
    def test_same_message_id_counted_once(self):
        # Verified against the Claude Code docs: one assistant event per
        # content block, same message id, same usage on each.
        g = Guard(quiet=True)
        u = {"input_tokens": 1000, "output_tokens": 50,
             "cache_read_input_tokens": 300, "cache_creation_input_tokens": 20}
        _feed(g, _assistant("msg_1", u, [{"type": "text", "text": "hi"}])
              + _assistant("msg_1", u, [{"type": "tool_use", "id": "t1",
                                         "name": "Agent", "input": {}}]))
        self.assertEqual(g.usage, {"input": 1000, "output": 50,
                                   "cache_write": 20, "cache_read": 300})
        self.assertEqual(g.usage_events, 2)

    def test_growing_output_is_topped_up(self):
        g = Guard(quiet=True)
        _feed(g, _assistant("m", {"input_tokens": 100, "output_tokens": 10})
              + _assistant("m", {"input_tokens": 100, "output_tokens": 40})
              + _assistant("m", {"input_tokens": 100, "output_tokens": 40}))
        self.assertEqual(g.usage["input"], 100)
        self.assertEqual(g.usage["output"], 40)

    def test_distinct_messages_add_up(self):
        g = Guard(quiet=True)
        _feed(g, _assistant("m1", {"input_tokens": 100, "output_tokens": 1})
              + _assistant("m2", {"input_tokens": 100, "output_tokens": 1}))
        self.assertEqual(g.usage["input"], 200)

    def test_events_without_message_id_are_each_counted(self):
        # older/other emitters (and sim/runaway.py) carry no message id
        g = Guard(quiet=True)
        line = json.dumps({"type": "assistant", "message": {
            "content": [], "usage": {"input_tokens": 100}}}) + "\n"
        _feed(g, line + line)
        self.assertEqual(g.usage["input"], 200)

    def test_agent_and_task_tool_names_both_count(self):
        for name in ("Agent", "Task"):
            obs = events.parse_line(_assistant(
                "m", {}, [{"type": "tool_use", "id": "t1", "name": name,
                           "input": {}}]))
            self.assertEqual(obs["spawns"], 1, name)
            self.assertEqual(obs["spawn_ids"], ["t1"], name)
        obs = events.parse_line(_assistant(
            "m", {}, [{"type": "tool_use", "id": "b", "name": "Bash",
                       "input": {}}]))
        self.assertEqual(obs["spawns"], 0)

    def test_spawn_block_counted_once_by_id(self):
        g = Guard(quiet=True)
        blk = {"type": "tool_use", "id": "toolu_1", "name": "Agent", "input": {}}
        _feed(g, _assistant("m", {}, [blk]) + _assistant("m", {}, [blk])
              + _assistant("m2", {}, [dict(blk, id="toolu_2")]))
        self.assertEqual(g.spawns, 2)

    def test_sim_spawn_count_unchanged(self):
        g = Guard(quiet=True)
        code, rec, _ = _run_guard(g, [PY, SIM, "--turns", "5", "--spawns", "4",
                                      "--tokens-per-turn", "100", "--delay", "0"])
        self.assertEqual(code, 0)
        self.assertEqual(rec["subagent_spawns"], 4)


# ---------------------------------------------------------------------
# L-6

NPM_SHIM = (
    "@ECHO off\r\nGOTO start\r\n:find_dp0\r\nSET dp0=%~dp0\r\nEXIT /b\r\n"
    ":start\r\nSETLOCAL\r\nCALL :find_dp0\r\n\r\n"
    "IF EXIST \"%dp0%\\node.exe\" (\r\n  SET \"_prog=%dp0%\\node.exe\"\r\n"
    ") ELSE (\r\n  SET \"_prog=node\"\r\n  SET PATHEXT=%PATHEXT:;.JS;=;%\r\n)\r\n\r\n"
    "endLocal & goto #_undefined_# 2>NUL || title %COMSPEC% & \"%_prog%\"  "
    "\"%dp0%\\node_modules\\@anthropic-ai\\claude-code\\cli.js\" %*\r\n")


class TestWindowsShimLaunch(unittest.TestCase):
    def _shim_dir(self, with_local_node: bool = True) -> tuple[str, str, str]:
        d = _tmp()
        shim = os.path.join(d, "claude.cmd")
        with open(shim, "w", encoding="utf-8", newline="") as f:
            f.write(NPM_SHIM)
        script = os.path.join(d, "node_modules", "@anthropic-ai", "claude-code",
                              "cli.js")
        os.makedirs(os.path.dirname(script))
        with open(script, "w", encoding="utf-8") as f:
            f.write("// cli\n")
        node = os.path.join(d, "node.exe")
        if with_local_node:
            with open(node, "wb") as f:
                f.write(b"MZ")
        return shim, script, node

    def test_npm_shim_resolves_to_node_script(self):
        shim, script, node = self._shim_dir()
        prompt = 'say "hi" to %USERNAME% ^ & echo pwned'
        cmd = launch.resolve_command([shim, "-p", prompt, "--verbose"],
                                     windows=True, which=lambda n: None)
        self.assertEqual(cmd, [node, script, "-p", prompt, "--verbose"])

    def test_npm_shim_without_local_node_uses_node_on_path(self):
        shim, script, _ = self._shim_dir(with_local_node=False)
        fake_node = os.path.join("C:\\", "Program Files", "nodejs", "node.exe")
        cmd = launch.resolve_command(
            [shim, "-p", "x"], windows=True,
            which=lambda n: fake_node if n == "node" else None)
        self.assertEqual(cmd[:2], [fake_node, script])

    def test_shim_whose_script_is_missing_falls_back(self):
        shim, script, _ = self._shim_dir()
        os.remove(script)
        # not resolvable: plain args still run the shim through cmd.exe...
        cmd = launch.resolve_command([shim, "-p", "plain"], windows=True,
                                     which=lambda n: None)
        self.assertEqual(cmd, [shim, "-p", "plain"])
        # ...but unsafe ones are refused
        with self.assertRaises(launch.LaunchError):
            launch.resolve_command([shim, "-p", "100% sure"], windows=True,
                                   which=lambda n: None)

    def test_other_cmd_refuses_each_unsafe_character(self):
        d = _tmp()
        bat = os.path.join(d, "tool.bat")
        with open(bat, "w", encoding="utf-8") as f:
            f.write("@echo off\r\necho %*\r\n")
        for ch in '"%!^&|<>\n':
            with self.assertRaises(launch.LaunchError, msg=repr(ch)) as cm:
                launch.resolve_command([bat, "--opt", f"a{ch}b"], windows=True,
                                       which=lambda n: None)
            self.assertIn("argument 2", str(cm.exception))
            self.assertIn("cmd.exe", str(cm.exception))
        self.assertEqual(
            launch.resolve_command([bat, "--opt", "plain text, fine"],
                                   windows=True, which=lambda n: None),
            [bat, "--opt", "plain text, fine"])

    def test_posix_never_rewrites_or_refuses(self):
        d = _tmp()
        bat = os.path.join(d, "tool.cmd")
        with open(bat, "w", encoding="utf-8") as f:
            f.write("echo %*\n")
        self.assertEqual(
            launch.resolve_command([bat, "100% & more"], windows=False,
                                   which=lambda n: None),
            [bat, "100% & more"])

    def test_which_resolves_argv0(self):
        cmd = launch.resolve_command(["claude", "-p", "x"], windows=False,
                                     which=lambda n: "/usr/bin/claude")
        self.assertEqual(cmd[0], "/usr/bin/claude")

    def test_refusal_is_a_clean_exit_2_without_receipt(self):
        g = Guard(quiet=True)
        tmp = _tmp()
        g.receipt_path = os.path.join(tmp, "r.json")
        err = io.StringIO()
        with mock.patch.object(launch, "resolve_command",
                               side_effect=launch.LaunchError("refusing: test")):
            with redirect_stderr(err):
                code = g.run(["claude.cmd", "-p", "%x%"])
        self.assertEqual(code, 2)
        self.assertIn("refusing: test", err.getvalue())
        self.assertNotIn("Traceback", err.getvalue())
        self.assertFalse(os.path.exists(g.receipt_path))

    def test_readme_is_honest_about_the_job_window_and_ctrl_c(self):
        with open(os.path.join(REPO, "README.md"), encoding="utf-8") as f:
            readme = f.read()
        self.assertNotIn("the moment it starts", readme)
        self.assertIn("immediately after launch", readme)
        self.assertIn("Ctrl-C", readme)
        self.assertIn("cmd.exe", readme)


# ---------------------------------------------------------------------
# L-7

class TestBanner(unittest.TestCase):
    def _banner(self, *args) -> str:
        err = io.StringIO()
        with redirect_stderr(err), redirect_stdout(io.StringIO()):
            cli_mod.main(["run", *args, "--history-path",
                          os.path.join(_tmp(), "h"), "--", PY, "-c", "pass"])
        return err.getvalue()

    def test_only_set_caps_are_printed(self):
        err = self._banner("--budget", "2", "--timeout", "30")
        self.assertIn("[shortleash] budget=$2 timeout=30s\n", err)
        self.assertNotIn("None", err)

    def test_no_caps_says_so(self):
        err = self._banner()
        self.assertIn("no caps set", err)
        self.assertNotIn("None", err)


# ---------------------------------------------------------------------
# L-8

class TestAgentErrorResult(unittest.TestCase):
    ERROR_CHILD = (
        "import sys, json; "
        "sys.stdout.write(json.dumps({'type': 'result', 'subtype': 'success', "
        "'is_error': True, 'result': 'Not logged in', 'total_cost_usd': 0.0, "
        "'num_turns': 1}) + chr(10))")

    def test_is_error_result_fails_the_run(self):
        g = Guard(quiet=True)
        code, rec, _ = _run_guard(g, [PY, "-c", self.ERROR_CHILD])
        self.assertEqual(code, AGENT_ERROR_EXIT_CODE)
        self.assertTrue(rec["agent_error"])
        self.assertEqual(rec["agent_error_text"], "Not logged in")
        self.assertFalse(rec["breached"])
        text = receipt_mod.format_human(rec)
        self.assertIn("FAILED:", text)
        self.assertIn("Not logged in", text)
        self.assertNotIn("✓", text)

    def test_child_nonzero_exit_wins_over_agent_error_code(self):
        g = Guard(quiet=True)
        code, rec, _ = _run_guard(
            g, [PY, "-c", self.ERROR_CHILD + "; raise SystemExit(7)"])
        self.assertEqual(code, 7)
        self.assertTrue(rec["agent_error"])

    def test_readme_install_and_no_alias(self):
        with open(os.path.join(REPO, "README.md"), encoding="utf-8") as f:
            readme = f.read()
        self.assertNotIn("<repo>", readme)
        self.assertIn("pip install git+https://github.com/calasdan-sketch/shortleash",
                      readme)
        self.assertNotIn("alias shortleash", readme)
        # README's claim about the ratchet must match the code
        self.assertNotIn("establishes its own baseline over time", readme)


# ---------------------------------------------------------------------
# L-9

class TestRedactPrompt(unittest.TestCase):
    def test_prompt_verbatim_by_default(self):
        argv = ["claude", "-p", "my password is hunter2"]
        self.assertEqual(receipt_mod.redact_argv(argv), argv)

    def test_redact_prompt_forms(self):
        r = receipt_mod.redact_argv
        self.assertEqual(r(["claude", "-p", "my password is hunter2",
                            "--output-format", "stream-json"], redact_prompt=True),
                         ["claude", "-p", "<redacted>", "--output-format",
                          "stream-json"])
        self.assertEqual(r(["claude", "--print", "secret stuff"], redact_prompt=True),
                         ["claude", "--print", "<redacted>"])
        self.assertEqual(r(["tool", "--prompt=secret stuff"], redact_prompt=True),
                         ["tool", "--prompt=<redacted>"])
        # -p followed by another flag: nothing to redact there
        self.assertEqual(r(["claude", "-p", "--verbose"], redact_prompt=True),
                         ["claude", "-p", "--verbose"])
        # secrets still redacted alongside
        self.assertEqual(r(["claude", "--api-key", "sk-1", "-p", "x"],
                           redact_prompt=True),
                         ["claude", "--api-key", "***", "-p", "<redacted>"])

    def test_receipt_end_to_end(self):
        tmp = _tmp()
        rp = os.path.join(tmp, "r.json")
        with redirect_stderr(io.StringIO()), redirect_stdout(io.StringIO()):
            cli_mod.main(["run", "--redact-prompt", "--quiet", "--receipt", rp,
                          "--history-path", os.path.join(tmp, "h"), "--",
                          PY, "-p", "my password is hunter2", "-c", "pass"])
        with open(rp, encoding="utf-8") as f:
            blob = f.read()
        self.assertNotIn("hunter2", blob)
        rec = json.loads(blob)
        self.assertTrue(rec["prompt_redacted"])
        self.assertEqual(rec["command"][1:3], ["-p", "<redacted>"])

    def test_history_stores_no_command_text(self):
        tmp = _tmp()
        hist = os.path.join(tmp, "h.jsonl")
        g = Guard(quiet=True, history_path=hist)
        g.receipt_path = os.path.join(tmp, "r.json")
        with redirect_stderr(io.StringIO()), redirect_stdout(io.StringIO()):
            g.run([PY, SIM, "--well-behaved"])
        with open(hist, encoding="utf-8") as f:
            entry = json.loads(f.readline())
        self.assertEqual(set(entry), {"agent", "profile", "cost_usd", "tokens",
                                      "breached", "ended_at"})


# ---------------------------------------------------------------------
# Simplifications

class TestSimplifications(unittest.TestCase):
    def test_load_pricing_gone_load_table_is_the_api(self):
        self.assertFalse(hasattr(pricing, "load_pricing"))
        self.assertIsInstance(pricing.load_table(None, environ={}),
                              pricing.PricingTable)

    def test_guard_takes_a_pricing_table_not_a_dict(self):
        g = Guard(quiet=True)
        self.assertIsInstance(g.pricing_table, pricing.PricingTable)
        t = pricing.PricingTable(rates={"default": (1.0, 1.0)})
        self.assertIs(Guard(quiet=True, pricing_table=t).pricing_table, t)

    def test_fleet_list_shows_holders(self):
        old = fleet_mod.DB_PATH
        fleet_mod.DB_PATH = os.path.join(_tmp(), "fleet.db")
        try:
            out = io.StringIO()
            with redirect_stdout(out):
                self.assertEqual(cli_mod.main(["fleet", "list"]), 0)
            self.assertIn("no active fleet claims", out.getvalue())
            conn = fleet_mod.connect()
            res = fleet_mod.claim(conn, [os.getcwd()], owner="dan",
                                  note="refactor", ttl=600)
            conn.close()
            out = io.StringIO()
            with redirect_stdout(out):
                self.assertEqual(cli_mod.main(["fleet", "list"]), 0)
            self.assertIn(res["claim_id"], out.getvalue())
            self.assertIn("held by dan", out.getvalue())
            self.assertIn("refactor", out.getvalue())
        finally:
            fleet_mod.DB_PATH = old

    def test_cache_is_a_separate_subcommand(self):
        parser = cli_mod._build_parser()
        out = io.StringIO()
        with redirect_stdout(out):
            with self.assertRaises(SystemExit):
                parser.parse_args(["--help"])
        self.assertIn("separate helper", out.getvalue())


if __name__ == "__main__":
    unittest.main()
