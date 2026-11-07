"""Process-tree control that works on Windows and POSIX.

The guard must be able to kill *everything* the supervised command
started, not just the direct child: an agent that spawns subagents or
shells and then exits (or gets killed) would otherwise leave
grandchildren running — still spending money — with our stdout pipe
held open.

One helper, two strategies:

POSIX   the child starts in its own session/process group
        (``start_new_session=True``), so its group id == its pid and
        ``os.killpg`` reaches every descendant that stayed in the group:
        SIGTERM, grace period, SIGKILL. The group id stays valid even
        after the child itself has exited and been reaped, as long as
        any grandchild is still alive.

Windows the child is placed in a Job Object right after it starts
        (``kernel32`` via ctypes, ``JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE``).
        Every process it starts inherits the job, so one
        ``TerminateJobObject`` kills the whole tree -- including orphans
        whose parent already exited -- and closing the job handle when the
        leash ends kills anything still left. There is no SIGTERM on
        Windows, so the grace period does not apply there. Only a child
        that breaks away from the job (``CREATE_BREAKAWAY_FROM_JOB``) is
        out of reach. If the job cannot be created or the child cannot be
        assigned, the fallback is ``taskkill /T /F /PID <pid>`` while the
        child is alive and, once it has exited, an orphan sweep by parent
        pid (Windows does not re-parent orphans).

Nothing here uses select(), fcntl, os.killpg on Windows or
signal.SIGKILL on Windows — the platform-specific names are only
touched on the platform that has them.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time

# subprocess.CREATE_NEW_PROCESS_GROUP exists only on Windows builds of
# Python; the numeric value is part of the Win32 API and never changes.
CREATE_NEW_PROCESS_GROUP = 0x00000200

#: How long (seconds) each taskkill / orphan query may take.
_CMD_TIMEOUT = 15.0


def is_windows() -> bool:
    return sys.platform == "win32" or os.name == "nt"


def popen_kwargs() -> dict:
    """Extra ``subprocess.Popen`` keywords that put the child in its own
    process group so :func:`kill_tree` can reach the whole tree."""
    if is_windows():
        return {"creationflags": CREATE_NEW_PROCESS_GROUP}
    return {"start_new_session": True}


#: prctl(2) option PR_SET_PDEATHSIG (Linux): the signal the kernel sends a
#: process when its parent dies.
_PR_SET_PDEATHSIG = 1


def parent_death_kwargs() -> dict:
    """Extra ``subprocess.Popen`` keywords that make the kernel SIGKILL the
    child when the leash dies. Linux only, best effort, and only the direct
    child (not its descendants): a SIGKILLed leash cannot clean up after
    itself, and without this the agent -- in its own session -- would run on,
    uncapped, with nobody watching. Anywhere else (macOS, Windows -- where
    the Job Object does it) or when libc cannot be reached it is ``{}``."""
    if not sys.platform.startswith("linux"):
        return {}
    try:
        import ctypes
        prctl = ctypes.CDLL(None, use_errno=True).prctl
        prctl.argtypes = [ctypes.c_int, ctypes.c_ulong, ctypes.c_ulong,
                          ctypes.c_ulong, ctypes.c_ulong]
        prctl.restype = ctypes.c_int
        sigkill = int(signal.SIGKILL)
    except Exception:
        return {}
    leash = os.getpid()

    def _die_with_the_leash() -> None:  # runs in the child, before exec
        try:
            prctl(_PR_SET_PDEATHSIG, sigkill, 0, 0, 0)
            if os.getppid() != leash:
                # the leash died before the call took effect
                os._exit(1)
        except Exception:
            pass

    return {"preexec_fn": _die_with_the_leash}


def taskkill_command(pid: int) -> list[str]:
    """Windows: force-kill ``pid`` and every process it started."""
    return ["taskkill", "/T", "/F", "/PID", str(int(pid))]


def children_command(pid: int) -> list[str]:
    """Windows: list the pids whose parent is ``pid`` (works after the
    parent has exited — Windows keeps the dead parent's pid on the
    orphan's record)."""
    return [
        "powershell", "-NoProfile", "-NonInteractive", "-Command",
        f"Get-CimInstance Win32_Process -Filter 'ParentProcessId={int(pid)}'"
        " | Select-Object -ExpandProperty ProcessId",
    ]


def _run_quiet(cmd: list[str]) -> subprocess.CompletedProcess | None:
    try:
        return subprocess.run(cmd, capture_output=True, text=True,
                              encoding="utf-8", errors="replace",
                              timeout=_CMD_TIMEOUT)
    except (OSError, subprocess.SubprocessError):
        return None


def _windows_children(pid: int) -> list[int]:
    p = _run_quiet(children_command(pid))
    if p is None:
        return []
    pids = []
    for tok in p.stdout.split():
        if tok.isdigit():
            pids.append(int(tok))
    return pids


def _kill_tree_windows(pid: int, child_exited: bool) -> int:
    """Returns the number of taskkill invocations that succeeded."""
    killed = 0
    if not child_exited:
        p = _run_quiet(taskkill_command(pid))
        if p is not None and p.returncode == 0:
            killed += 1
        return killed
    # The root is already gone: taskkill cannot find its tree, so sweep
    # the orphans by parent pid (each with its own /T for *their* trees).
    for cpid in _windows_children(pid):
        p = _run_quiet(taskkill_command(cpid))
        if p is not None and p.returncode == 0:
            killed += 1
    return killed


# -- processes that left the group (Linux) ---------------------------------
#
# A process group is not a container: a grandchild that calls setsid() (a
# daemon, `nohup setsid`, a tool that detaches on purpose) leaves it and a
# group kill never reaches it. On Linux the leash can still find it by
# ancestry: it reads /proc, remembers who descends from the agent while they
# are descended from it, and, being a "child subreaper" (PR_SET_CHILD_
# SUBREAPER), becomes the parent of anything orphaned below the agent
# instead of init, so those stay findable after their parent is killed.
# Elsewhere (macOS, Windows -- where the Job Object already covers it) all
# of this does nothing.

#: Set to any value to switch off the sentinel and the by-ancestry sweep
#: (the benchmarks use it to measure what they add).
NO_ESCAPE_ENV = "SHORTLEASH_NO_ESCAPE_SWEEP"
_PR_SET_CHILD_SUBREAPER = 36
_subreaper = False
#: How many detached processes the last kill_tree() found and killed.
detached_killed = 0


def _linux() -> bool:
    return sys.platform.startswith("linux") and os.path.isdir("/proc/self")


_baseline_children: "set[int]" = set()


def _prctl_subreaper(flag: int) -> bool:
    import ctypes
    prctl = ctypes.CDLL(None, use_errno=True).prctl
    prctl.argtypes = [ctypes.c_int, ctypes.c_ulong, ctypes.c_ulong,
                      ctypes.c_ulong, ctypes.c_ulong]
    prctl.restype = ctypes.c_int
    return prctl(_PR_SET_CHILD_SUBREAPER, flag, 0, 0, 0) == 0


def set_subreaper() -> bool:
    """Make the leash the adoptive parent of orphans below the agent
    (Linux, best effort). True when it took effect. The children the
    process already has are remembered and never swept."""
    global _subreaper, _baseline_children
    if not _linux() or os.environ.get(NO_ESCAPE_ENV):
        return False
    try:
        table = process_table()
        me = os.getpid()
        _baseline_children = {p for p, v in table.items() if v[0] == me}
        _subreaper = _prctl_subreaper(1)
    except Exception:
        _subreaper = False
    return _subreaper


#: The agent the current run is guarding: only a kill aimed at THIS agent
#: sweeps what the leash adopted (a late kill meant for an earlier run in the
#: same process must never touch the current run's processes).
_agent_pid: "int | None" = None


def note_agent(pid: int) -> None:
    global _agent_pid
    _agent_pid = pid


def clear_subreaper() -> None:
    global _subreaper, _agent_pid
    if _subreaper:
        reap_adopted_zombies(protected | ({_agent_pid} if _agent_pid else set()))
    _agent_pid = None
    if _subreaper:
        try:
            _prctl_subreaper(0)
        except Exception:
            pass
    _subreaper = False


def process_table() -> dict:
    """{pid: (ppid, pgrp, sid, starttime, state)} read from /proc (Linux); {} when
    that is not possible. Start time makes (pid, start) an identity that a
    recycled pid does not share."""
    table: dict = {}
    if not _linux():
        return table
    try:
        names = os.listdir("/proc")
    except OSError:
        return table
    for name in names:
        if not name.isdigit():
            continue
        try:
            with open(f"/proc/{name}/stat", "rb") as f:
                raw = f.read().decode("utf-8", "replace")
            rest = raw[raw.rindex(")") + 2:].split()
            table[int(name)] = (int(rest[1]), int(rest[2]), int(rest[3]),
                                int(rest[19]), rest[0])
        except (OSError, ValueError, IndexError):
            continue  # gone, or unreadable
    return table


def descendants(root: int, table: dict) -> dict:
    """{pid: starttime} of every process below ``root`` by ancestry."""
    kids: dict = {}
    for pid, (ppid, _pg, _sid, _st, _state) in table.items():
        kids.setdefault(ppid, []).append(pid)
    out: dict = {}
    stack = list(kids.get(root, []))
    while stack:
        pid = stack.pop()
        if pid in out or pid not in table:
            continue
        out[pid] = table[pid][3]
        stack.extend(kids.get(pid, []))
    return out


def _kill_identified(victims: dict) -> int:
    """SIGKILL each (pid -> starttime) that is still the same process."""
    now = process_table()
    n = 0
    for pid, st in victims.items():
        if pid in now and now[pid][3] == st and pid != os.getpid():
            try:
                os.kill(pid, signal.SIGKILL)
                n += 1
            except OSError:
                pass
    return n


def reap_adopted_zombies(exclude: "set[int]") -> None:
    """Collect the zombies of adopted orphans that died (we became their
    parent when the agent went; nobody else will wait for them)."""
    if not _subreaper:
        return
    me = os.getpid()
    for pid, v in process_table().items():
        if (v[0] == me and v[4] == "Z" and pid not in exclude
                and pid not in _baseline_children):
            try:
                os.waitpid(pid, os.WNOHANG)
            except OSError:
                pass


def escaped_below(agent_pid: int) -> dict:
    """{pid: starttime} of the agent's descendants that are NOT in its
    process group (detached with setsid) right now."""
    table = process_table()
    return {pid: st for pid, st in descendants(agent_pid, table).items()
            if table[pid][1] != agent_pid}


def sweep_adopted(exclude: "set[int]") -> int:
    """Kill what the leash adopted and its subtrees (orphans of the agent;
    needs set_subreaper). ``exclude``: pids that are ours to keep."""
    if not _subreaper:
        return 0
    me = os.getpid()
    table = process_table()
    victims: dict = {}
    for pid, (ppid, _pg, _sid, st, _state) in table.items():
        if ppid == me and pid not in exclude and pid not in _baseline_children:
            victims[pid] = st
            victims.update(descendants(pid, table))
    n = _kill_identified(victims)
    if n:
        time.sleep(0.05)
    reap_adopted_zombies(exclude)
    return n


# -- the sentinel: the tree dies with the leash, even on kill -9 -----------
#
# PR_SET_PDEATHSIG (see parent_death_kwargs) takes only the agent itself. A
# second small process, started in its own session, holds the read end of a
# pipe whose write end only the leash has. When the leash ends without saying
# it is done -- kill -9, the OOM killer, a crash -- the pipe closes, and the
# sentinel SIGKILLs the agent's process group and every descendant it has
# been tracking (once a second, by ancestry and start time, so a setsid'd
# daemon is included on Linux). On a normal finish the leash writes "d" and
# the sentinel exits without touching anything.

_SENTINEL_CODE = r"""
import os, select, signal, sys
leash = int(sys.argv[1])
for _s in (signal.SIGINT, signal.SIGHUP, signal.SIGQUIT):
    try:
        signal.signal(_s, signal.SIG_IGN)
    except Exception:
        pass

def table():
    t = {}
    try:
        names = os.listdir("/proc")
    except OSError:
        return t
    for n in names:
        if n.isdigit():
            try:
                raw = open("/proc/%s/stat" % n, "rb").read().decode("utf-8", "replace")
                r = raw[raw.rindex(")") + 2:].split()
                t[int(n)] = (int(r[1]), int(r[2]), int(r[19]))
            except Exception:
                pass
    return t

# The leash starts us BEFORE the agent (starting a process takes a few ms
# the agent's first lines should not wait for) and then writes "p<pid>\n".
buf = b""
while b"\n" not in buf:
    ready, _, _ = select.select([0], [], [], 30.0)
    if not ready:
        sys.exit(0)
    data = os.read(0, 64)
    if not data:
        sys.exit(0)        # the leash ended before it launched anything
    buf += data
agent = int(buf.split(b"\n", 1)[0].lstrip(b"p"))
pgid = agent
rest = buf.split(b"\n", 1)[1]
if rest.startswith(b"d"):
    sys.exit(0)

known = {}
start0 = None
baseline = None

def refresh():
    global start0, baseline
    t = table()
    if start0 is None and agent in t:
        start0 = t[agent][2]
    kids = {}
    for pid, (pp, pg, st) in t.items():
        kids.setdefault(pp, []).append(pid)
    if baseline is None:
        # the leash's children from before the agent started (passed in)
        baseline = set(int(x) for x in sys.argv[2].split(",") if x)
        baseline.add(os.getpid())
        baseline.discard(agent)
    # the agent, and what the leash adopted when the agent's orphans lost
    # their parent (it is a child subreaper on Linux)
    stack = [agent] + [p for p in kids.get(leash, []) if p not in baseline]
    while stack:
        p = stack.pop()
        if p in t:
            known[p] = t[p][2]
            stack.extend(kids.get(p, []))
    for pid, (pp, pg, st) in t.items():
        if pg == pgid and (start0 is None or st >= start0):
            known[pid] = st

refresh()
while True:
    try:
        ready, _, _ = select.select([0], [], [], 1.0)
    except InterruptedError:
        continue
    if ready:
        data = os.read(0, 16)
        if data.startswith(b"d"):
            sys.exit(0)
        if not data:
            break
        continue
    refresh()
refresh()
t = table()
for pid, st in known.items():
    if pid in t and t[pid][2] == st and pid != os.getpid():
        try:
            os.kill(pid, signal.SIGKILL)
        except Exception:
            pass
try:
    os.killpg(pgid, signal.SIGKILL)
except Exception:
    pass
"""


class Sentinel:
    def __init__(self, proc: subprocess.Popen, wfd: int):
        self.proc = proc
        self.wfd = wfd

    @property
    def pid(self) -> int:
        return self.proc.pid

    def attach(self, agent_pid: int) -> None:
        """Tell the sentinel which process group it is guarding."""
        try:
            os.write(self.wfd, b"p%d\n" % agent_pid)
        except OSError:
            pass

    def done(self) -> None:
        """The run ended normally: tell the sentinel to stand down."""
        try:
            os.write(self.wfd, b"d")
        except OSError:
            pass
        try:
            os.close(self.wfd)
        except OSError:
            pass
        deadline = time.monotonic() + 3.0
        while self.proc.poll() is None and time.monotonic() < deadline:
            time.sleep(0.01)
        if self.proc.poll() is None:
            try:
                self.proc.kill()
                while self.proc.poll() is None:
                    time.sleep(0.01)
            except Exception:
                pass
        protected.discard(self.proc.pid)


def start_sentinel() -> "Sentinel | None":
    """Start the sentinel (POSIX); ``attach(pid)`` then names the agent whose
    process group it guards. None on Windows (the kill-on-close Job Object does this) or when
    it cannot be started: the run is then guarded as before."""
    if is_windows() or os.environ.get(NO_ESCAPE_ENV):
        return None
    try:
        r, w = os.pipe()
    except OSError:
        return None
    try:
        proc = subprocess.Popen(
            [sys.executable, "-I", "-c", _SENTINEL_CODE, str(os.getpid()),
             ",".join(str(p) for p in sorted(_baseline_children))],
            stdin=r, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True, close_fds=True)
    except Exception:
        os.close(r)
        os.close(w)
        return None
    os.close(r)
    protected.add(proc.pid)
    return Sentinel(proc, w)


#: Pids the leash must never sweep (its own helpers, e.g. the sentinel).
protected: "set[int]" = set()


def _signal_group(pgid: int, sig: int) -> bool:
    try:
        os.killpg(pgid, sig)  # noqa: no-member on Windows; never reached there
        return True
    except (ProcessLookupError, PermissionError, OSError):
        return False


def _kill_tree_posix(proc: subprocess.Popen, grace: float,
                     child_exited: bool) -> int:
    global detached_killed
    detached_killed = 0
    off = bool(os.environ.get(NO_ESCAPE_ENV))
    detached = {} if child_exited or off else escaped_below(proc.pid)
    n = _kill_group(proc, grace, child_exited)
    # Whatever left the group (setsid) is killed by identity, then whatever
    # that left orphaned and the leash adopted.
    detached_killed = (_kill_identified(detached) if detached else 0)
    if proc.pid == _agent_pid:
        detached_killed += sweep_adopted(protected | {proc.pid})
    return n + (1 if detached_killed else 0)


def _kill_group(proc: subprocess.Popen, grace: float,
                child_exited: bool) -> int:
    pgid = proc.pid  # start_new_session=True => pgid == pid
    if not _signal_group(pgid, signal.SIGTERM):
        return 0  # group already empty: nothing escaped
    # A stopped process (paused at an approval prompt, or Ctrl-Z'd) only
    # acts on SIGTERM once it runs again: wake the group so it can.
    _signal_group(pgid, signal.SIGCONT)
    deadline = time.monotonic() + max(0.0, grace)
    while time.monotonic() < deadline:
        if not child_exited:
            proc.poll()  # reap the direct child as soon as it goes
        if not _signal_group(pgid, 0):
            return 1  # signal 0 = probe: nobody left in the group
        time.sleep(0.05)
    _signal_group(pgid, signal.SIGKILL)
    return 1


def can_pause() -> bool:
    """Can the agent's tree be paused (SIGSTOP / SIGCONT to its group)?"""
    return not is_windows() and hasattr(signal, "SIGSTOP")


def pause_tree(proc: subprocess.Popen) -> bool:
    """Stop every process of the agent's group (POSIX). True on success."""
    if not can_pause():
        return False
    return _signal_group(proc.pid, signal.SIGSTOP)


def resume_tree(proc: subprocess.Popen) -> bool:
    if not can_pause():
        return False
    return _signal_group(proc.pid, signal.SIGCONT)


# -- Windows Job Objects --------------------------------------------------

JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
_JobObjectBasicAccountingInformation = 1
_JobObjectExtendedLimitInformation = 9


def _win_api():
    """kernel32 bound through ctypes with the structures we need.
    Returns None anywhere ctypes.WinDLL is unavailable (i.e. not Windows)."""
    try:
        import ctypes
        from ctypes import wintypes
        k = ctypes.WinDLL("kernel32", use_last_error=True)
    except Exception:
        return None

    class IO_COUNTERS(ctypes.Structure):
        _fields_ = [(n, ctypes.c_uint64) for n in (
            "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
            "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]

    class JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_int64),
            ("PerJobUserTimeLimit", ctypes.c_int64),
            ("LimitFlags", wintypes.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
        ]

    class JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", JOBOBJECT_BASIC_LIMIT_INFORMATION),
            ("IoInfo", IO_COUNTERS),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    class JOBOBJECT_BASIC_ACCOUNTING_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("TotalUserTime", ctypes.c_int64),
            ("TotalKernelTime", ctypes.c_int64),
            ("ThisPeriodTotalUserTime", ctypes.c_int64),
            ("ThisPeriodTotalKernelTime", ctypes.c_int64),
            ("TotalPageFaultCount", wintypes.DWORD),
            ("TotalProcesses", wintypes.DWORD),
            ("ActiveProcesses", wintypes.DWORD),
            ("TotalTerminatedProcesses", wintypes.DWORD),
        ]

    k.CreateJobObjectW.argtypes = [wintypes.LPVOID, wintypes.LPCWSTR]
    k.CreateJobObjectW.restype = wintypes.HANDLE
    k.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int,
                                          wintypes.LPVOID, wintypes.DWORD]
    k.SetInformationJobObject.restype = wintypes.BOOL
    k.QueryInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int,
                                            wintypes.LPVOID, wintypes.DWORD,
                                            wintypes.LPDWORD]
    k.QueryInformationJobObject.restype = wintypes.BOOL
    k.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    k.AssignProcessToJobObject.restype = wintypes.BOOL
    k.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
    k.TerminateJobObject.restype = wintypes.BOOL
    k.CloseHandle.argtypes = [wintypes.HANDLE]
    k.CloseHandle.restype = wintypes.BOOL
    return (k, ctypes, JOBOBJECT_EXTENDED_LIMIT_INFORMATION,
            JOBOBJECT_BASIC_ACCOUNTING_INFORMATION)


class WindowsJob:
    """A kill-on-close Job Object that holds the supervised child and
    every process it starts. Create it with :meth:`create`, :meth:`assign`
    the child right after ``Popen``, :meth:`terminate` to kill the tree,
    :meth:`close` when the leash is done (closing kills whatever is left).
    All methods are no-raise; failures return False/None."""

    def __init__(self, handle, api):
        self.handle = handle
        self._api = api

    @classmethod
    def create(cls) -> "WindowsJob | None":
        if not is_windows():
            return None
        api = _win_api()
        if api is None:
            return None
        k, ctypes, EXT, _ACCT = api
        try:
            h = k.CreateJobObjectW(None, None)
            if not h:
                return None
            info = EXT()
            info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            ok = k.SetInformationJobObject(h, _JobObjectExtendedLimitInformation,
                                           ctypes.byref(info), ctypes.sizeof(info))
            if not ok:
                k.CloseHandle(h)
                return None
            return cls(h, api)
        except Exception:
            return None

    def assign(self, proc: subprocess.Popen) -> bool:
        """Put ``proc`` (and, from now on, its descendants) into the job."""
        if self.handle is None:
            return False
        k = self._api[0]
        ph = getattr(proc, "_handle", None)  # Popen's process HANDLE on Windows
        if ph is None:
            return False
        try:
            return bool(k.AssignProcessToJobObject(self.handle, int(ph)))
        except Exception:
            return False

    def active_processes(self) -> int | None:
        if self.handle is None:
            return None
        k, ctypes, _EXT, ACCT = self._api
        try:
            acct = ACCT()
            ok = k.QueryInformationJobObject(self.handle,
                                             _JobObjectBasicAccountingInformation,
                                             ctypes.byref(acct),
                                             ctypes.sizeof(acct), None)
            return int(acct.ActiveProcesses) if ok else None
        except Exception:
            return None

    def terminate(self) -> bool:
        if self.handle is None:
            return False
        try:
            return bool(self._api[0].TerminateJobObject(self.handle, 1))
        except Exception:
            return False

    def close(self) -> None:
        h, self.handle = self.handle, None
        if h is not None:
            try:
                self._api[0].CloseHandle(h)
            except Exception:
                pass


def attach_job(proc: subprocess.Popen) -> "WindowsJob | None":
    """Windows: create a job and assign ``proc`` to it. Returns the job,
    or None when not on Windows or when the job could not be set up (the
    caller then falls back to taskkill)."""
    job = WindowsJob.create()
    if job is None:
        return None
    if not job.assign(proc):
        job.close()
        return None
    return job


def close_job(job: "WindowsJob | None") -> None:
    if job is not None:
        job.close()


def _kill_tree_job(job: WindowsJob, child_exited: bool) -> int:
    active = job.active_processes()
    if active == 0:
        return 0  # nothing escaped
    if child_exited and active is None:
        # Cannot tell; terminating an empty job is harmless.
        pass
    return 1 if job.terminate() else 0


def kill_tree(proc: subprocess.Popen, grace: float = 5.0,
              child_exited: bool | None = None,
              job: "WindowsJob | None" = None) -> int:
    """Kill ``proc`` and every process it started.

    ``child_exited`` says whether the direct child is already gone (then
    only its escaped descendants are targeted); None means "look it up".
    ``job`` is the :class:`WindowsJob` the child was assigned to, if any.
    Returns a count of successful kill operations (0 = nothing to kill
    or nothing we could reach). Never raises.
    """
    if child_exited is None:
        child_exited = proc.poll() is not None
    try:
        if is_windows():
            if job is not None and job.handle is not None:
                return _kill_tree_job(job, child_exited)
            return _kill_tree_windows(proc.pid, child_exited)
        return _kill_tree_posix(proc, grace, child_exited)
    except Exception:
        return 0
