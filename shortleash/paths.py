"""Where shortleash keeps its state (history, fleet DB).

Default: ``~/.shortleash`` (``%USERPROFILE%\\.shortleash`` on Windows).
Override with the ``SHORTLEASH_HOME`` environment variable — tests set
it to a temp dir so they never touch the real user's files. Resolved at
call time, never at import time, so the env var is honoured even when it
is set after the module was imported.
"""

from __future__ import annotations

import os
import threading

STATE_ENV = "SHORTLEASH_HOME"


def state_dir() -> str:
    custom = os.environ.get(STATE_ENV)
    if custom:
        return os.path.abspath(os.path.expanduser(custom))
    return os.path.join(os.path.expanduser("~"), ".shortleash")


def history_path() -> str:
    return os.path.join(state_dir(), "history.jsonl")


def fleet_db_path() -> str:
    return os.path.join(state_dir(), "fleet.db")


def private_dir(path: str) -> None:
    """Create ``path`` (and missing parents) readable by this user only
    (0700 on POSIX; Windows ignores the mode). A folder that already exists
    is tightened when it is ours; one we cannot change is left alone."""
    path = os.path.abspath(path)
    missing = []
    here = path
    while not os.path.isdir(here) and os.path.dirname(here) != here:
        missing.append(here)
        here = os.path.dirname(here)
    for d in reversed(missing):          # every folder we create is private
        try:
            os.mkdir(d, 0o700)
        except FileExistsError:
            pass
    os.makedirs(path, exist_ok=True)     # raises if it is a file, etc.
    if os.name == "posix":
        try:
            st = os.stat(path)
            if st.st_uid == os.getuid() and st.st_mode & 0o077:
                os.chmod(path, 0o700)
        except OSError:
            pass


_APPEND_LOCK = threading.Lock()
#: a byte far beyond any state file; Windows lets a region past the end of a
#: file be locked, so readers and the writes themselves are never blocked by it
_WIN_LOCK_OFFSET = 0x7FFFFF00


def _win_lock(fd: int, lock: bool) -> None:
    import msvcrt
    os.lseek(fd, _WIN_LOCK_OFFSET, os.SEEK_SET)
    if not lock:
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        return
    for _ in range(600):                 # LK_LOCK itself retries ~10 s
        try:
            msvcrt.locking(fd, msvcrt.LK_LOCK, 1)
            return
        except OSError:
            continue
    raise OSError("could not lock the state file for appending")


def append_private(path: str, data: bytes) -> None:
    """Append ``data`` to ``path`` as ONE record: two threads or processes
    appending at once interleave whole records, never halves, and none is
    lost. The file is created 0600.

    POSIX: a single write() to an O_APPEND file is atomic. Windows: the C
    runtime fakes append by seeking to the end and then writing, so two
    writers can both seek to the same end and overwrite each other; there
    the seek-and-write is done under an OS byte-range lock (msvcrt.locking)
    and, inside this process, a mutex."""
    flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND | getattr(os, "O_BINARY", 0)
    with _APPEND_LOCK:
        fd = os.open(path, flags, 0o600)
        try:
            if os.name == "nt":
                _win_lock(fd, True)
                try:
                    os.lseek(fd, 0, os.SEEK_END)
                    os.write(fd, data)
                finally:
                    try:
                        _win_lock(fd, False)
                    except OSError:
                        pass
            else:
                os.write(fd, data)
        finally:
            os.close(fd)
