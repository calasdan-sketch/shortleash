"""Process-tree helper: Windows vs POSIX strategy, and the escaped-child
regression (panel round 3).

- On win32 the child is put in a kill-on-close Job Object so one call
  kills the whole tree, orphans included (TestWindowsJob runs the real
  kernel32 calls, Windows only). The taskkill fallback -- new process
  group, `taskkill /T /F /PID`, orphan sweep by parent pid once the child
  has exited -- is exercised by monkeypatching sys.platform/os.name and
  subprocess.run, so its command construction is verified everywhere.
- Escaped grandchild: a child that spawns `python -c "time.sleep(60)"`
  (inheriting our stdout pipe) and exits used to hang the leash forever
  on proc.stdout.close() and leave the grandchild running. The leash must
  return promptly and the grandchild must be dead.
"""

import _isolate  # noqa: F401  (isolate ~/.shortleash before importing)

import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import types
import unittest
from unittest import mock

from shortleash import proctree
from shortleash.guard import Guard, BREACH_EXIT_CODE

PY = sys.executable


def _fake_popen(pid: int, returncode=None):
    return types.SimpleNamespace(pid=pid, poll=lambda: returncode)


class TestWindowsStrategy(unittest.TestCase):
    """Command construction for win32, checked via monkeypatching."""

    def setUp(self):
        self._p = mock.patch.object(sys, "platform", "win32")
        self._n = mock.patch.object(os, "name", "nt")
        self._p.start()
        self._n.start()

    def tearDown(self):
        self._p.stop()
        self._n.stop()

    def test_popen_kwargs_new_process_group(self):
        self.assertTrue(proctree.is_windows())
        kw = proctree.popen_kwargs()
        self.assertEqual(kw, {"creationflags": proctree.CREATE_NEW_PROCESS_GROUP})
        self.assertEqual(proctree.CREATE_NEW_PROCESS_GROUP, 0x200)
        self.assertNotIn("start_new_session", kw)

    def test_taskkill_command(self):
        self.assertEqual(proctree.taskkill_command(4242),
                         ["taskkill", "/T", "/F", "/PID", "4242"])

    def test_kill_tree_live_child_uses_taskkill_tree(self):
        calls = []

        def fake_run(cmd, **kw):
            calls.append((cmd, kw))
            return subprocess.CompletedProcess(cmd, 0, "", "")

        with mock.patch.object(proctree.subprocess, "run", fake_run):
            n = proctree.kill_tree(_fake_popen(4242), grace=0.1,
                                   child_exited=False)
        self.assertEqual(n, 1)
        self.assertEqual([c for c, _ in calls],
                         [["taskkill", "/T", "/F", "/PID", "4242"]])
        # every subprocess call carries a timeout (never hang on taskkill)
        self.assertTrue(all(kw.get("timeout") for _, kw in calls))

    def test_kill_tree_dead_child_sweeps_orphans_by_parent_pid(self):
        calls = []

        def fake_run(cmd, **kw):
            calls.append(cmd)
            self.assertIn("timeout", kw)
            if cmd[0] == "powershell":
                return subprocess.CompletedProcess(cmd, 0, "501\r\n502\r\n", "")
            return subprocess.CompletedProcess(cmd, 0, "", "")

        with mock.patch.object(proctree.subprocess, "run", fake_run):
            n = proctree.kill_tree(_fake_popen(4242, returncode=0), grace=0.1)
        self.assertEqual(n, 2)
        self.assertEqual(calls[0][:4],
                         ["powershell", "-NoProfile", "-NonInteractive",
                          "-Command"])
        self.assertIn("ParentProcessId=4242", calls[0][4])
        self.assertEqual(calls[1:], [["taskkill", "/T", "/F", "/PID", "501"],
                                     ["taskkill", "/T", "/F", "/PID", "502"]])

    def test_kill_tree_never_raises(self):
        def boom(cmd, **kw):
            raise OSError("taskkill missing")

        with mock.patch.object(proctree.subprocess, "run", boom):
            self.assertEqual(proctree.kill_tree(_fake_popen(1), grace=0.1,
                                                child_exited=False), 0)


@unittest.skipUnless(proctree.is_windows(), "real Job Object calls need Windows")
class TestWindowsJob(unittest.TestCase):
    """The primary Windows strategy, run against kernel32 for real."""

    def test_job_kills_child_and_grandchild_after_child_exited(self):
        job = proctree.WindowsJob.create()
        self.assertIsNotNone(job, "CreateJobObjectW failed")
        try:
            proc = subprocess.Popen(
                [PY, "-c",
                 "import subprocess,sys,time;"
                 "p=subprocess.Popen([sys.executable,'-c','import time;time.sleep(60)']);"
                 "print(p.pid,flush=True);time.sleep(0.5)"],
                stdout=subprocess.PIPE, text=True, **proctree.popen_kwargs())
            self.assertTrue(job.assign(proc), "AssignProcessToJobObject failed")
            gpid = int(proc.stdout.readline().strip())
            proc.wait(timeout=20)  # the child exits; the grandchild lingers
            self.assertTrue(_pid_alive(gpid))
            self.assertGreaterEqual(job.active_processes() or 0, 1)
            self.assertEqual(
                proctree.kill_tree(proc, child_exited=True, job=job), 1)
            self.assertTrue(_wait_dead(gpid, 5), "grandchild survived the job")
            self.assertEqual(job.active_processes(), 0)
            self.assertEqual(
                proctree.kill_tree(proc, child_exited=True, job=job), 0)
        finally:
            job.close()
            try:
                _kill_pid(gpid)
            except Exception:
                pass

    def test_close_kills_whatever_is_left(self):
        job = proctree.WindowsJob.create()
        self.assertIsNotNone(job)
        proc = subprocess.Popen([PY, "-c", "import time;time.sleep(60)"],
                                **proctree.popen_kwargs())
        try:
            self.assertTrue(job.assign(proc))
            job.close()
            self.assertIsNotNone(proc.wait(timeout=10))
        finally:
            _kill_pid(proc.pid)

    def test_attach_job_round_trip(self):
        proc = subprocess.Popen([PY, "-c", "import time;time.sleep(60)"],
                                **proctree.popen_kwargs())
        try:
            job = proctree.attach_job(proc)
            self.assertIsNotNone(job)
            self.assertEqual(job.active_processes(), 1)
            self.assertTrue(job.terminate())
            self.assertIsNotNone(proc.wait(timeout=10))
            job.close()
            self.assertIsNone(job.handle)
            self.assertFalse(job.terminate())  # closed: no-raise False
        finally:
            _kill_pid(proc.pid)


class TestNoJobOffWindows(unittest.TestCase):
    @unittest.skipIf(proctree.is_windows(), "POSIX-only expectation")
    def test_attach_job_is_none_on_posix(self):
        self.assertIsNone(proctree.WindowsJob.create())
        proc = subprocess.Popen([PY, "-c", "pass"])
        try:
            self.assertIsNone(proctree.attach_job(proc))
            proctree.close_job(None)  # no-op
        finally:
            proc.wait(timeout=10)


class TestPosixStrategy(unittest.TestCase):
    def test_popen_kwargs_new_session(self):
        with mock.patch.object(sys, "platform", "linux"), \
                mock.patch.object(os, "name", "posix"):
            self.assertFalse(proctree.is_windows())
            self.assertEqual(proctree.popen_kwargs(),
                             {"start_new_session": True})


# ---------------------------------------------------------------------
# Escaped grandchild (real processes).

# Child: spawn a grandchild that inherits our stdout and sleeps 60s, write
# its pid to argv[1], emit one telemetry line, exit 0.
ESCAPING_CHILD = (
    "import subprocess, sys, json; "
    "p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)']); "
    "open(sys.argv[1], 'w', encoding='utf-8').write(str(p.pid)); "
    "sys.stdout.write(json.dumps({'type': 'system', 'subtype': 'init', "
    "'model': 'claude-sonnet-4-5', 'session_id': 'x'}) + chr(10)); "
    "sys.stdout.flush(); "
    "sys.exit(0)"
)

# Child: same grandchild, but the child itself then sleeps — the --timeout
# kill must take both.
LINGERING_CHILD = (
    "import subprocess, sys, time; "
    "p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)']); "
    "open(sys.argv[1], 'w', encoding='utf-8').write(str(p.pid)); "
    "sys.stdout.flush(); "
    "time.sleep(60)"
)


def _pid_alive(pid: int) -> bool:
    if proctree.is_windows():
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
        time.sleep(0.1)
    return not _pid_alive(pid)


def _kill_pid(pid: int) -> None:
    try:
        if proctree.is_windows():
            subprocess.run(proctree.taskkill_command(pid), capture_output=True,
                           timeout=30)
        else:
            import signal
            os.kill(pid, signal.SIGKILL)
    except Exception:
        pass


class TestEscapedGrandchild(unittest.TestCase):
    def _run_bounded(self, guard: Guard, child_src: str, bound: float):
        tmp = tempfile.mkdtemp()
        pidfile = os.path.join(tmp, "gc.pid")
        guard.receipt_path = os.path.join(tmp, "r.json")
        guard.history_path = os.path.join(tmp, "h.jsonl")
        result = {}

        def target():
            result["code"] = guard.run([PY, "-c", child_src, pidfile])

        t = threading.Thread(target=target, daemon=True)
        start = time.monotonic()
        t.start()
        t.join(bound)
        elapsed = time.monotonic() - start
        gpid = None
        try:
            with open(pidfile, encoding="utf-8") as f:
                gpid = int(f.read().strip() or 0) or None
        except OSError:
            pass
        if t.is_alive():
            # Regressed: unblock the leash so it cannot outlive the suite.
            if gpid:
                _kill_pid(gpid)
            t.join(10)
            self.fail(f"leash did not return within {bound}s "
                      f"(escaped grandchild held the output pipe open)")
        return result["code"], elapsed, gpid

    def test_child_exits_but_grandchild_holds_pipe(self):
        g = Guard(timeout=30.0, grace_period=0.5, quiet=True, anomaly=None)
        code, elapsed, gpid = self._run_bounded(g, ESCAPING_CHILD, bound=20)
        try:
            self.assertIsNotNone(gpid, "child never reported its grandchild pid")
            self.assertLess(elapsed, 15, "leash took too long to return")
            self.assertEqual(code, 0)  # the child itself exited cleanly
            self.assertTrue(_wait_dead(gpid, 5),
                            "grandchild survived: tree was not killed")
            self.assertTrue(g.escaped)
            with open(g.receipt_path, encoding="utf-8") as f:
                rec = json.load(f)
            self.assertTrue(any("child process" in w for w in rec["warnings"]),
                            rec["warnings"])
        finally:
            if gpid:
                _kill_pid(gpid)

    def test_timeout_kills_child_and_grandchild(self):
        g = Guard(timeout=1.0, grace_period=0.5, quiet=True, anomaly=None)
        code, elapsed, gpid = self._run_bounded(g, LINGERING_CHILD, bound=20)
        try:
            self.assertEqual(code, BREACH_EXIT_CODE)
            self.assertIn("time limit", g.breach_reason or "")
            self.assertIsNotNone(gpid)
            self.assertTrue(_wait_dead(gpid, 5),
                            "grandchild survived the timeout kill")
            self.assertLess(elapsed, 15)
        finally:
            if gpid:
                _kill_pid(gpid)


if __name__ == "__main__":
    unittest.main()
