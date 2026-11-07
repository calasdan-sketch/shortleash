"""A spend ledger shared by every agent on the machine.

``--budget`` caps ONE run or session. Real money leaks between them: ten
sessions at $9 each stay under a $10 cap and still cost $90. The ledger
adds caps over *all* of them, per local calendar day (``--daily-budget``)
and per project (``--project-daily-budget``), and works across ``run`` and
``hook`` alike.

Design -- built so concurrent agents cannot lose each other's writes and
nothing needs a lock:

- every run / session owns ONE file, ``<state>/ledger/<id>.jsonl``, and only
  ever APPENDS to it, one short line (a single ``write`` of well under a
  pipe-buffer) holding the CUMULATIVE cost so far: ``{"t": epoch seconds,
  "c": cost, "p": project}``. Nobody rewrites a line, so there is no
  read-modify-write to race on;
- the spend of one id on one day is the cumulative cost at the end of the
  day minus the cumulative cost at its start, so a session that crosses
  midnight is split, not double counted;
- the total is the sum over the files touched that day. A reader that sees a
  half-written last line skips it (it parses the next time).

Limits, stated: a run's FIRST line carries everything it had spent before
it was first seen (a session that predates the hook counts in full on the
day it is first seen: an over-count, the safe side); the cost in it is the
same estimate the other caps use; two agents can each pass the check just
before the other's spend lands, so the cap can be overshot by about what
the agents spend between two of their own checks (one tool call for a hook,
one second for ``run``); and it is a guardrail against a runaway, not a
defence against an agent that deletes the ledger.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time

from . import paths

LEDGER_DIR = "ledger"
#: A new line is appended only when the cost grew by this much (USD), so a
#: long session writes a few thousand short lines at most, not one per call.
MIN_STEP = 0.005
#: Files older than this many days are removed when the ledger is written.
KEEP_DAYS = 45


def ledger_dir() -> str:
    return os.path.join(paths.state_dir(), LEDGER_DIR)


def run_id(raw: str) -> str:
    """A safe file name for any session / run id (never a path)."""
    return hashlib.sha256(str(raw).encode("utf-8", "replace")).hexdigest()[:32]


def project_of(transcript_path: str) -> str:
    """Claude Code keeps a project's transcripts together in one directory
    named after the project's path: that directory's name is the key."""
    name = os.path.basename(os.path.dirname(os.path.abspath(transcript_path)))
    return re.sub(r"[\x00-\x1f\x7f]", "", name)[:200]


def project_of_dir(directory: str) -> str:
    """The project key of a working directory, spelled the way Claude Code
    names a project's transcript directory (every character that is not a
    letter or digit becomes a dash) so `run` and `hook` agree."""
    return re.sub(r"[^A-Za-z0-9]", "-", os.path.abspath(directory))[:200]


def day_bounds(now: float | None = None) -> tuple[float, float]:
    """Epoch seconds of the start and end of the local day holding ``now``."""
    now = time.time() if now is None else now
    lt = time.localtime(now)
    start = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 0, 0, 0, 0, 0, -1))
    nxt = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday + 1, 0, 0, 0, 0, 0, -1))
    return start, nxt


def _read_lines(path: str) -> list[dict]:
    out = []
    try:
        with open(path, "rb") as f:
            data = f.read()
    except OSError:
        return out
    for raw in data.split(b"\n"):
        if not raw.strip():
            continue
        try:
            d = json.loads(raw.decode("utf-8", "replace"))
        except (ValueError, RecursionError):
            continue  # a half-written line
        if (isinstance(d, dict) and isinstance(d.get("t"), (int, float))
                and isinstance(d.get("c"), (int, float))
                and not isinstance(d["c"], bool) and d["c"] == d["c"]
                and 0 <= d["c"] < 1e12):
            out.append(d)
    return out


def _last_cost(path: str) -> float | None:
    """The cumulative cost of the last whole line of a ledger file, reading
    only its end."""
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - 1024))
            tail = f.read()
    except OSError:
        return None
    for raw in reversed(tail.split(b"\n")):
        try:
            d = json.loads(raw.decode("utf-8", "replace"))
            c = d["c"]
            if (isinstance(c, (int, float)) and not isinstance(c, bool)
                    and c == c and 0 <= c < 1e12
                    and isinstance(d["t"], (int, float))):
                return float(c)
        except (ValueError, KeyError, TypeError, RecursionError):
            continue
    return None


def record(raw_id: str, cost: float, project: str = "",
           now: float | None = None) -> bool:
    """Append ``cost`` (cumulative, USD) for this run / session when it grew
    by at least MIN_STEP. Returns True when a line was written. Raises
    OSError when the ledger cannot be written (the caller decides whether
    that is fatal); never writes a figure that is not finite."""
    if not (isinstance(cost, (int, float)) and not isinstance(cost, bool)
            and cost == cost
            and 0 <= cost < 1e12):
        return False
    d = ledger_dir()
    os.makedirs(d, exist_ok=True)
    path = os.path.join(d, run_id(raw_id) + ".jsonl")
    last = _last_cost(path)
    if last is not None and cost < last + MIN_STEP:
        return False
    line = json.dumps({"t": round(time.time() if now is None else now, 3),
                       "c": round(float(cost), 6), "p": project},
                      separators=(",", ":")) + "\n"
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    try:
        os.write(fd, line.encode("utf-8"))
    finally:
        os.close(fd)
    if last is None:
        _prune(d)
    return True


def _prune(d: str) -> None:
    cutoff = time.time() - KEEP_DAYS * 86400
    try:
        names = os.listdir(d)
    except OSError:
        return
    for name in names[:500]:
        full = os.path.join(d, name)
        try:
            if name.endswith(".jsonl") and os.stat(full).st_mtime < cutoff:
                os.remove(full)
        except OSError:
            pass


def spend_between(start: float, end: float, project: str | None = None,
                  exclude: str | None = None) -> float:
    """USD spent by every id (``exclude`` = one id left out) between two
    epoch times, optionally only the runs of one project."""
    d = ledger_dir()
    skip = run_id(exclude) + ".jsonl" if exclude is not None else None
    total = 0.0
    try:
        names = os.listdir(d)
    except OSError:
        return 0.0
    for name in names:
        if not name.endswith(".jsonl") or name == skip:
            continue
        full = os.path.join(d, name)
        try:
            if os.stat(full).st_mtime < start:
                continue  # nothing was appended since the window began
        except OSError:
            continue
        lines = _read_lines(full)
        if not lines:
            continue
        if project is not None and lines[-1].get("p") != project:
            continue
        before = 0.0
        upto = 0.0
        for ln in lines:
            if ln["t"] < start:
                before = max(before, float(ln["c"]))
            if ln["t"] < end:
                upto = max(upto, float(ln["c"]))
        total += max(0.0, upto - before)
    return total


def today_spend(project: str | None = None, exclude: str | None = None,
                now: float | None = None) -> float:
    start, end = day_bounds(now)
    return spend_between(start, end, project, exclude)


def check_writable() -> str | None:
    """None when the ledger can be used, else why not (for a fail-closed
    start: a cap that cannot be checked must not be listed as enforced)."""
    d = ledger_dir()
    try:
        os.makedirs(d, exist_ok=True)
        probe = os.path.join(d, f".probe-{os.getpid()}")
        with open(probe, "w", encoding="utf-8"):
            pass
        os.remove(probe)
    except OSError as e:
        return f"{d}: {e.strerror or e}"
    return None
