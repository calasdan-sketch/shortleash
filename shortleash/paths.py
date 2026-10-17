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
