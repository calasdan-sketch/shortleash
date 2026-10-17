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


def _signal_group(pgid: int, sig: int) -> bool:
    try:
        os.killpg(pgid, sig)  # noqa: no-member on Windows; never reached there
        return True
    except (ProcessLookupError, PermissionError, OSError):
        return False


def _kill_tree_posix(proc: subprocess.Popen, grace: float,
                     child_exited: bool) -> int:
    import signal  # local: keeps SIGKILL out of the Windows code path
    pgid = proc.pid  # start_new_session=True => pgid == pid
    if not _signal_group(pgid, signal.SIGTERM):
        return 0  # group already empty: nothing escaped
    deadline = time.monotonic() + max(0.0, grace)
    while time.monotonic() < deadline:
        if not child_exited:
            proc.poll()  # reap the direct child as soon as it goes
        if not _signal_group(pgid, 0):
            return 1  # signal 0 = probe: nobody left in the group
        time.sleep(0.05)
    _signal_group(pgid, signal.SIGKILL)
    return 1


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
