"""Fleet claims for `shortleash run --fleet`.

Merged from claimlock (retired as a standalone): a lockfile for agents.
The wrapper claims paths before the run, warns (doesn't block) on
conflict, and releases on exit — via finally/atexit/signal handlers,
with TTL as the backstop for hard kills.

Deliberately minimal: claim/release/overlap/TTL. No broadcasts, no
fleet-wide messaging (unauthenticated injection channel — killed on
purpose).

The claims live in a SQLite database, and `sqlite3` is imported only when the
fleet is first used (see _sqlite3): a Python built without it (some pyenv and
Alpine builds) must still run everything else, `--version` included.
"""

from __future__ import annotations

import os
import time
import uuid
from typing import TYPE_CHECKING

from . import paths

if TYPE_CHECKING:  # annotations only: the real import is lazy, see _sqlite3()
    import sqlite3

#: Explicit override hook for the DB location (tests, embedding). When
#: None, the path comes from paths.fleet_db_path() — i.e. ~/.shortleash
#: or $SHORTLEASH_HOME — resolved at connect() time.
DB_PATH: str | None = None
DEFAULT_TTL = 1800  # 30 minutes
MAX_TTL = 86400  # 24 hours


class FleetUnavailable(Exception):
    """Fleet claims cannot work in this Python: it has no `sqlite3` module.
    The message says so and is meant to be shown as it is."""


def _sqlite3():
    """The `sqlite3` module, imported on first use, or FleetUnavailable."""
    try:
        import sqlite3
    except ImportError as e:  # no _sqlite3 C module in this Python build
        raise FleetUnavailable(
            f"fleet claims need Python's sqlite3 module, and this Python was "
            f"built without it ({e})") from None
    return sqlite3


def db_errors() -> tuple:
    """The exceptions that mean "the fleet database cannot be used" (a corrupt
    file, a directory that cannot be written): what connect() and the claim
    functions raise for reasons of their own."""
    try:
        return (_sqlite3().Error, OSError)
    except FleetUnavailable:
        return (OSError,)


SCHEMA = """
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS claims (
    rowid INTEGER PRIMARY KEY,
    claim_id TEXT,
    path TEXT,
    owner TEXT,
    note TEXT,
    expires_at INTEGER,
    created_at INTEGER
);
CREATE INDEX IF NOT EXISTS idx_claims_path ON claims(path);
CREATE INDEX IF NOT EXISTS idx_claims_cid ON claims(claim_id);
"""


def connect(db_path: str | None = None) -> sqlite3.Connection:
    sqlite3 = _sqlite3()
    # NB: resolve the path at call time, not as a default arg — a default
    # arg would bind the value at import and ignore $SHORTLEASH_HOME or a
    # later DB_PATH reassignment.
    if db_path is None:
        db_path = DB_PATH or paths.fleet_db_path()
    os.makedirs(os.path.dirname(os.path.abspath(db_path)), exist_ok=True)
    conn = sqlite3.connect(db_path, timeout=5.0)
    conn.executescript(SCHEMA)
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def _now() -> int:
    return int(time.time())


def prune(conn: sqlite3.Connection) -> int:
    cur = conn.execute("DELETE FROM claims WHERE expires_at <= ?", (_now(),))
    conn.commit()
    return cur.rowcount


def norm(path: str) -> str:
    return os.path.realpath(os.path.abspath(os.path.expanduser(path)))


def _overlaps(a: str, b: str) -> bool:
    if a == b:
        return True
    return a.startswith(b + os.sep) or b.startswith(a + os.sep)


def _active(conn: sqlite3.Connection) -> list[dict]:
    cur = conn.execute("SELECT DISTINCT claim_id, path, owner, note,"
                       " expires_at, created_at FROM claims"
                       " ORDER BY created_at")
    return [{"claim_id": cid, "path": p, "owner": o, "note": n,
             "expires_at": e, "created_at": c,
             "ttl_left": max(0, e - _now())}
            for cid, p, o, n, e, c in cur.fetchall()]


def active_claims(conn: sqlite3.Connection) -> list[dict]:
    prune(conn)
    return _active(conn)


def claim(conn: sqlite3.Connection, paths: list[str], owner: str = "",
          note: str = "", ttl: int = DEFAULT_TTL) -> dict:
    """Try to claim paths. Returns {ok, claim_id?, conflicts?}.

    Uses BEGIN IMMEDIATE + in-transaction recheck so two agents racing
    the same path can't both win.
    """
    if ttl <= 0:
        raise ValueError("ttl must be positive seconds")
    ttl = min(ttl, MAX_TTL)
    normed = [norm(p) for p in paths]
    conn.execute("BEGIN IMMEDIATE")
    try:
        # Prune inside the transaction without committing — the final
        # COMMIT/ROLLBACK settles it. Calling prune() here would commit
        # early and break the race atomicity.
        conn.execute("DELETE FROM claims WHERE expires_at <= ?", (_now(),))
        existing = _active(conn)
        conflicts = []
        for p in normed:
            for c in existing:
                if _overlaps(p, c["path"]):
                    conflicts.append({"path": p, "holder": c})
                    break
        if conflicts:
            conn.execute("ROLLBACK")
            return {"ok": False, "conflicts": conflicts}
        cid = uuid.uuid4().hex[:12]
        exp = _now() + ttl
        conn.executemany(
            "INSERT INTO claims (claim_id, path, owner, note,"
            " expires_at, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            [(cid, p, owner, note, exp, _now()) for p in normed])
        conn.execute("COMMIT")
    except Exception:
        try:
            conn.execute("ROLLBACK")
        except Exception:
            pass
        raise
    return {"ok": True, "claim_id": cid, "paths": normed,
            "expires_in": ttl}


def release(conn: sqlite3.Connection, claim_id: str) -> dict:
    """Release by capability token (the claim_id). No path-based release:
    only the holder of the token can release."""
    prune(conn)
    cur = conn.execute("DELETE FROM claims WHERE claim_id = ?", (claim_id,))
    conn.commit()
    return {"released": cur.rowcount}
