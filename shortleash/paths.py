"""Where shortleash keeps its state (history, fleet DB).

Default: ``~/.shortleash`` (``%USERPROFILE%\\.shortleash`` on Windows).
Override with the ``SHORTLEASH_HOME`` environment variable — tests set
it to a temp dir so they never touch the real user's files. Resolved at
call time, never at import time, so the env var is honoured even when it
is set after the module was imported.
"""

from __future__ import annotations

import os

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


def append_private(path: str, data: bytes) -> None:
    """Append ``data`` to ``path`` with ONE write (two processes appending
    at once interleave whole records, never halves); the file is created
    0600."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        os.write(fd, data)
    finally:
        os.close(fd)
