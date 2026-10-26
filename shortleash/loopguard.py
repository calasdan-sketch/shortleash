"""Repeat-call guard for Claude Code hook mode.

A coding agent that is stuck often repeats one tool call: the same failing
command, the same file read, the same edit. Each repeat costs a full turn.
This module spots that from the hook input, before the call runs.

    * Every tool call becomes a FINGERPRINT: the tool name plus a SHA-256 of
      its canonical input (keys sorted, runs of white space collapsed, long
      structures bounded). Only the fingerprint, the tool name, the
      tool_use_id and a time are stored; never the input itself.
    * A ring buffer of the last ``window`` calls of a session is kept in a
      small private file next to the spawn-limit state.
    * ``warn`` identical calls inside that window: the call is ALLOWED and
      Claude is told (additionalContext) and so is the user (systemMessage).
      No permissionDecision is sent for a warning, because "allow" would also
      skip the user's permission prompt.
    * ``deny`` identical calls inside the window: permissionDecision "deny"
      with a reason Claude reads. 0 switches a level off.
    * Optional: if ``velocity_usd_per_min`` is set and the session
      transcript is readable, the estimated cost of the last five minutes is
      compared with it, and a warning (never a denial) is sent at most once
      every ten minutes.

What it does not do, plainly: it sees identical calls, not merely similar
ones (a loop that changes one character each time is not caught); it
cannot take back tokens already spent; the velocity figure is an estimate
from the price table and the transcript, which is a file Claude Code does
not document; and, like the rest of hook mode, it fails OPEN: any problem
is an allow. It counts per session_id.
"""

from __future__ import annotations

import datetime
import hashlib
import json
import os
import re
import time

from . import paths

#: Defaults: conservative. Warn at the 4th identical call among the last 20,
#: deny at the 8th.
DEFAULT_WARN = 4
DEFAULT_DENY = 8
DEFAULT_WINDOW = 20
#: Calls older than this are not part of the window (a session picked up
#: again after a break does not inherit yesterday's repeats).
MAX_AGE_SECONDS = 3600
#: The most lines ever read from a ring-buffer file.
MAX_LINES = 5000
#: Canonical-input bounds: nothing here may grow without limit.
MAX_DEPTH = 24
MAX_ITEMS = 500
MAX_STRING = 200_000

VELOCITY_WINDOW_SECONDS = 300
VELOCITY_REWARN_SECONDS = 600
#: Only the end of a transcript is read.
TRANSCRIPT_TAIL_BYTES = 1 << 20

ENV_WARN = "SHORTLEASH_LOOP_WARN"
ENV_DENY = "SHORTLEASH_LOOP_DENY"
ENV_WINDOW = "SHORTLEASH_LOOP_WINDOW"
ENV_VELOCITY = "SHORTLEASH_VELOCITY_USD_PER_MIN"
ENV_OFF = "SHORTLEASH_LOOP_GUARD"          # "off" / "0" / "false" disables


# ---------------------------------------------------------------------------
# Fingerprints
# ---------------------------------------------------------------------------

_WS = re.compile(r"\s+")


def _norm(value, depth=0):
    if depth > MAX_DEPTH:
        return "<deep>"
    if isinstance(value, str):
        return _WS.sub(" ", value[:MAX_STRING]).strip()
    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, (int, float)):
        return value if value == value else "nan"
    if isinstance(value, dict):
        out = {}
        for i, key in enumerate(sorted(value, key=str)):
            if i >= MAX_ITEMS:
                break
            out[str(key)] = _norm(value[key], depth + 1)
        return out
    if isinstance(value, (list, tuple)):
        return [_norm(v, depth + 1) for v in list(value)[:MAX_ITEMS]]
    return str(type(value).__name__)


def fingerprint(tool_name: str, tool_input) -> str:
    """tool name + first 16 hex digits of SHA-256 of the canonical input."""
    text = json.dumps(_norm(tool_input), sort_keys=True,
                      separators=(",", ":"), ensure_ascii=True)
    digest = hashlib.sha256(text.encode("ascii")).hexdigest()[:16]
    return f"{tool_name[:60]}:{digest}"


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------

def _whole(value, minimum=0):
    if isinstance(value, bool):
        return None
    if isinstance(value, str):
        try:
            value = int(value.strip())
        except ValueError:
            return None
    if isinstance(value, float) and value == int(value):
        value = int(value)
    return value if isinstance(value, int) and value >= minimum else None


def _positive(value):
    if isinstance(value, bool):
        return None
    if isinstance(value, str):
        try:
            value = float(value.strip())
        except ValueError:
            return None
    if isinstance(value, (int, float)) and value == value \
            and 0 < value < float("inf"):
        return float(value)
    return None


def settings_from(raw: dict | None) -> dict:
    """The valid keys of a {"warn","deny","window","velocity"} dict;
    anything unusable is dropped (and so falls back to a lower source)."""
    out: dict = {}
    raw = raw if isinstance(raw, dict) else {}
    for key in ("warn", "deny", "window"):
        value = _whole(raw.get(key))
        if value is not None:
            out[key] = value
    value = _positive(raw.get("velocity"))
    if value is not None:
        out["velocity"] = value
    return out


def from_environ(environ) -> dict | None:
    """Settings from the environment; None = nothing set. ``{"off": True}``
    when $SHORTLEASH_LOOP_GUARD says off."""
    flag = (environ.get(ENV_OFF) or "").strip().lower()
    if flag in ("off", "0", "false", "no"):
        return {"off": True}
    got = settings_from({"warn": environ.get(ENV_WARN),
                         "deny": environ.get(ENV_DENY),
                         "window": environ.get(ENV_WINDOW),
                         "velocity": environ.get(ENV_VELOCITY)})
    return got or None


def finish(sources: list) -> dict | None:
    """Combine settings dicts, strongest first, into the working config
    ({"warn","deny","window","velocity"}), or None when no source asked for
    the guard (it is then off). The window is never smaller than the
    deepest level it has to count to."""
    merged: dict = {}
    asked = False
    for source in sources:
        if not source:
            continue
        if source.get("off"):
            return None
        asked = True
        for key, value in source.items():
            merged.setdefault(key, value)
    if not asked:
        return None
    cfg = {"warn": merged.get("warn", DEFAULT_WARN),
           "deny": merged.get("deny", DEFAULT_DENY),
           "window": merged.get("window", DEFAULT_WINDOW),
           "velocity": merged.get("velocity")}
    cfg["window"] = max(cfg["window"], cfg["warn"], cfg["deny"], 2)
    return cfg


def problems(cfg: dict) -> list[str]:
    """Reasons a config given at install time makes no sense (empty = fine)."""
    out = []
    if cfg["warn"] and cfg["deny"] and cfg["deny"] <= cfg["warn"]:
        out.append(f"the deny level ({cfg['deny']}) must be higher than the "
                   f"warn level ({cfg['warn']})")
    if not cfg["warn"] and not cfg["deny"] and not cfg.get("velocity"):
        out.append("warn, deny and velocity are all off; nothing would "
                   "be checked")
    return out


# ---------------------------------------------------------------------------
# The ring buffer
# ---------------------------------------------------------------------------

def _read(path: str) -> list[dict]:
    rows: list[dict] = []
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for i, line in enumerate(f):
                if i >= MAX_LINES:
                    break
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if isinstance(row, dict):
                    rows.append(row)
    except OSError:
        pass
    return rows


def _rewrite(path: str, rows: list[dict]) -> None:
    """Replace the file with ``rows`` (atomic, still 0600)."""
    tmp = f"{path}.{os.getpid()}.tmp"
    data = "".join(json.dumps(r, separators=(",", ":")) + "\n"
                   for r in rows).encode("utf-8")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, data)
    finally:
        os.close(fd)
    try:
        os.replace(tmp, path)
    except OSError:
        try:
            os.unlink(tmp)
        except OSError:
            pass


def record_and_count(path: str, fp: str, tool: str, use_id: str,
                     window: int, now: float | None = None) -> int:
    """Add one call to the session's ring buffer and return how many of the
    last ``window`` calls (this one included, and only calls from the last
    hour) have the same fingerprint. A tool_use_id seen before (Claude Code
    retrying one call) is not counted twice."""
    now = time.time() if now is None else now
    paths.private_dir(os.path.dirname(path))
    rows = [r for r in _read(path)
            if isinstance(r.get("ts"), (int, float))
            and now - r["ts"] <= MAX_AGE_SECONDS and "fp" in r]
    seen = bool(use_id) and any(
        r.get("id") == use_id for r in rows)
    if not seen:
        row = {"fp": fp, "tool": tool[:40], "ts": round(now, 3)}
        if use_id:
            row["id"] = use_id[:80]
        paths.append_private(path, (json.dumps(
            row, separators=(",", ":")) + "\n").encode("utf-8"))
        rows.append(row)
        if len(rows) > window * 4 + 16:
            _rewrite(path, rows[-window:])
    recent = rows[-window:]
    return sum(1 for r in recent if r.get("fp") == fp)


def level(count: int, cfg: dict) -> str:
    """"deny", "warn" or "ok" for ``count`` identical calls."""
    if cfg["deny"] and count >= cfg["deny"]:
        return "deny"
    if cfg["warn"] and count >= cfg["warn"]:
        return "warn"
    return "ok"


def warn_text(count: int, tool: str, cfg: dict) -> str:
    deny = (f" It will be refused at {cfg['deny']}." if cfg["deny"] else "")
    return (f"shortleash loop guard: this is the {count}th identical "
            f"{tool} call among your last {cfg['window']} tool calls.{deny} "
            f"If it keeps giving the same result, stop repeating it: read "
            f"the result, change the approach, or stop and tell the user "
            f"what is blocking you.")


def deny_text(count: int, tool: str, cfg: dict) -> str:
    return (f"shortleash loop guard: this identical {tool} call has now "
            f"been made {count} times among your last {cfg['window']} "
            f"tool calls, which is the limit. It was not run. Do something "
            f"different (change the command, fix the cause of the error), "
            f"or stop and report to the user what is blocking you. The "
            f"limit was set by the user.")


# ---------------------------------------------------------------------------
# Spend velocity (optional)
# ---------------------------------------------------------------------------

def _when(text) -> float | None:
    try:
        d = datetime.datetime.fromisoformat(str(text).replace("Z", "+00:00"))
    except ValueError:
        return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=datetime.timezone.utc)
    return d.timestamp()


def _tail_lines(path: str) -> list[str]:
    with open(path, "rb") as f:
        f.seek(0, os.SEEK_END)
        size = f.tell()
        start = max(0, size - TRANSCRIPT_TAIL_BYTES)
        f.seek(start)
        data = f.read(TRANSCRIPT_TAIL_BYTES)
    if start:
        data = data.split(b"\n", 1)[-1]        # the first line is cut
    return data.decode("utf-8", "replace").splitlines()


def spend_rate(transcript_path, now: float | None = None,
               window: int = VELOCITY_WINDOW_SECONDS) -> float | None:
    """Estimated USD per minute over the last ``window`` seconds, from the
    assistant messages at the end of a Claude Code transcript, priced with
    shortleash's own table. None when there is no readable cost data. The
    transcript format is not documented: anything unexpected is skipped."""
    if not isinstance(transcript_path, str) or not transcript_path:
        return None
    from . import pricing           # local import: only this path needs it
    now = time.time() if now is None else now
    try:
        lines = _tail_lines(transcript_path)
    except OSError:
        return None
    by_id: dict[str, tuple[float, str, dict]] = {}
    for n, line in enumerate(lines):
        if not line.startswith("{"):
            continue
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        if not isinstance(ev, dict) or ev.get("type") != "assistant":
            continue
        message = ev.get("message")
        if not isinstance(message, dict) or not isinstance(
                message.get("usage"), dict):
            continue
        ts = _when(ev.get("timestamp"))
        if ts is None or ts < now - window or ts > now + 60:
            continue
        usage = message["usage"]
        counts = {"input": _whole(usage.get("input_tokens")) or 0,
                  "output": _whole(usage.get("output_tokens")) or 0,
                  "cache_write": _whole(
                      usage.get("cache_creation_input_tokens")) or 0,
                  "cache_read": _whole(
                      usage.get("cache_read_input_tokens")) or 0}
        key = str(message.get("id") or f"line-{n}")
        by_id[key] = (ts, str(message.get("model") or ""), counts)
    if not by_id:
        return None
    cost = 0.0
    for ts, model, counts in by_id.values():
        cost += pricing.estimate_cost(model, counts)
    earliest = min(ts for ts, _m, _c in by_id.values())
    minutes = max(1.0, min(window, now - earliest) / 60.0)
    return cost / minutes


def velocity_text(rate: float, limit: float) -> str:
    return (f"shortleash: this session is spending an estimated "
            f"${rate:.2f} per minute over the last five minutes, above "
            f"your limit of ${limit:.2f} per minute (an estimate from the "
            f"transcript and shortleash's price table, not a bill). "
            f"Check that the work is worth it, or that nothing is looping.")


def velocity_due(path: str, now: float) -> bool:
    """Has no velocity warning been sent for this session in the last ten
    minutes? (Recorded in the ring-buffer file as a {"vel": ts} line.)"""
    last = 0.0
    for r in _read(path):
        if isinstance(r.get("vel"), (int, float)):
            last = max(last, r["vel"])
    return now - last >= VELOCITY_REWARN_SECONDS


def note_velocity(path: str, now: float) -> None:
    paths.private_dir(os.path.dirname(path))
    paths.append_private(path, (json.dumps(
        {"vel": round(now, 3)}) + "\n").encode("utf-8"))
