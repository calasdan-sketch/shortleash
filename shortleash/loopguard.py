"""Repeat-call guard for Claude Code hook mode.

A coding agent that is stuck often repeats one tool call: the same failing
command, the same file read, the same edit. Each repeat costs a full turn.
This module spots that from the hook input, before the call runs.

    * Every tool call becomes a FINGERPRINT: the tool name plus a SHA-256 of
      its whole input (keys sorted, hashed piece by piece, nothing cut off).
      White space is collapsed only in a Bash command, where it does not
      change the meaning; every other tool's input is compared exactly (an
      edit that differs by indentation is a different edit).
    * Only the fingerprint, the tool name, the tool_use_id and a time are
      stored, never the input. One small append-only file per session.
    * Default metric, "consecutive": the same call N times IN A ROW. A
      different call in between starts the count again, so an ordinary
      edit / test / edit / test cycle is never counted. The optional
      "window" metric counts identical calls among the last W calls.
    * Warning (default 6 in a row): the call is ALLOWED and Claude and the
      user are told. No permissionDecision is sent for a warning, because
      "allow" would also skip the user's permission prompt.
    * Refusal is OFF unless you set it (--loop-deny N, at least 12 unless you
      force a lower number): permissionDecision "deny" with a reason Claude
      reads. A refused call is not recorded, so one different call in between
      ends the refusal at once.
    * Optional: if ``velocity_usd_per_min`` is set and the session
      transcript is readable, the estimated cost of the last five minutes is
      compared with it, and a warning (never a denial) is sent at most once
      every ten minutes.

What it does not do, plainly: it sees identical calls, not similar ones; it
cannot tell a loop from legitimate polling or a test that is re-run after an
edit elsewhere (it has no PostToolUse, so it never sees results), which is why
refusing is opt-in; it cannot take back tokens already spent; the velocity
figure is an estimate from the price table and the transcript, which is a file
Claude Code does not document; and, like the rest of hook mode, it fails OPEN:
any problem is an allow. It counts per session_id.
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
DEFAULT_WARN = 6                  # consecutive mode
DEFAULT_WARN_WINDOW = 4           # window mode
DEFAULT_DENY = 0                  # refusing is opt-in
DEFAULT_WINDOW = 20
MIN_DENY = 12                     # lowest --loop-deny without --force-low
MODES = ("consecutive", "window")
DEFAULT_MODE = "consecutive"
#: Calls older than this are not part of the window (a session picked up
#: again after a break does not inherit yesterday's repeats).
MAX_AGE_SECONDS = 3600
#: Only the end of a ring-buffer file is read (it is append-only).
TAIL_BYTES = 256 * 1024
#: A file bigger than this stops growing (the guard then sees nothing new).
MAX_FILE_BYTES = 16 * 1024 * 1024
#: Nesting deeper than this is hashed as a marker.
MAX_DEPTH = 24
#: Strings are fed to the hash in pieces of this many characters.
CHUNK = 65536

VELOCITY_WINDOW_SECONDS = 300
VELOCITY_REWARN_SECONDS = 600
#: Only the end of a transcript is read.
TRANSCRIPT_TAIL_BYTES = 1 << 20

ENV_WARN = "SHORTLEASH_LOOP_WARN"
ENV_DENY = "SHORTLEASH_LOOP_DENY"
ENV_WINDOW = "SHORTLEASH_LOOP_WINDOW"
ENV_VELOCITY = "SHORTLEASH_VELOCITY_USD_PER_MIN"
ENV_MODE = "SHORTLEASH_LOOP_MODE"
ENV_OFF = "SHORTLEASH_LOOP_GUARD"          # "off" / "0" / "false" disables


# ---------------------------------------------------------------------------
# Fingerprints
# ---------------------------------------------------------------------------

_WS = re.compile(r"\s+")
#: Tools whose "command" is shell text, where white space has no meaning
#: between words. Nothing else is normalised.
SHELL_TOOLS = ("Bash", "PowerShell")


def _feed(h, value, depth, collapse):
    if depth > MAX_DEPTH:
        h.update(b"<deep>")
        return
    if isinstance(value, str):
        if collapse:
            value = _WS.sub(" ", value).strip()
        h.update(b"s%d:" % len(value))
        for i in range(0, len(value), CHUNK):
            h.update(value[i:i + CHUNK].encode("utf-8", "surrogatepass"))
    elif isinstance(value, bool):
        h.update(b"T" if value else b"F")
    elif value is None:
        h.update(b"N")
    elif isinstance(value, (int, float)):
        h.update(b"n" + repr(value).encode("ascii") + b";")
    elif isinstance(value, dict):
        h.update(b"{%d:" % len(value))
        for key in sorted(value, key=str):
            _feed(h, str(key), depth + 1, False)
            _feed(h, value[key], depth + 1, False)
        h.update(b"}")
    elif isinstance(value, (list, tuple)):
        h.update(b"[%d:" % len(value))
        for item in value:
            _feed(h, item, depth + 1, False)
        h.update(b"]")
    else:
        h.update(b"?" + type(value).__name__.encode("ascii", "replace"))


def fingerprint(tool_name: str, tool_input) -> str:
    """tool name + first 16 hex digits of SHA-256 over the whole input.
    White space is collapsed only in a shell tool's ``command``."""
    h = hashlib.sha256()
    if tool_name in SHELL_TOOLS and isinstance(tool_input, dict):
        h.update(b"{%d:" % len(tool_input))
        for key in sorted(tool_input, key=str):
            _feed(h, str(key), 1, False)
            _feed(h, tool_input[key], 1, key == "command")
        h.update(b"}")
    else:
        _feed(h, tool_input, 0, False)
    return f"{tool_name[:60]}:{h.hexdigest()[:16]}"


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
    mode = raw.get("mode")
    if isinstance(mode, str) and mode.strip().lower() in MODES:
        out["mode"] = mode.strip().lower()
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
                         "velocity": environ.get(ENV_VELOCITY),
                         "mode": environ.get(ENV_MODE)})
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
    mode = merged.get("mode", DEFAULT_MODE)
    cfg = {"mode": mode,
           "warn": merged.get("warn", DEFAULT_WARN if mode == "consecutive"
                              else DEFAULT_WARN_WINDOW),
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
# The call log: append-only, only the end of it is read
# ---------------------------------------------------------------------------

def _read(path: str) -> list[dict]:
    rows: list[dict] = []
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            start = max(0, size - TAIL_BYTES)
            f.seek(start)
            data = f.read(TAIL_BYTES)
    except OSError:
        return rows
    if start:
        data = data.split(b"\n", 1)[-1]          # the first line is cut
    for line in data.decode("utf-8", "replace").splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows


def count_for(path: str, fp: str, tool: str, use_id: str, cfg: dict,
              now: float | None = None) -> tuple[int, str]:
    """(how many identical calls this one makes, "deny" | "warn" | "ok").
    The call is recorded unless it is refused: a refused call leaves no
    trace, so one different call in between ends the refusal at once. A
    tool_use_id seen before (Claude Code retrying one call) is not counted
    twice. Append-only (one write per call), so two calls made at the same
    moment cannot lose each other's line."""
    now = time.time() if now is None else now
    paths.private_dir(os.path.dirname(path))
    rows = [r for r in _read(path)
            if isinstance(r.get("ts"), (int, float))
            and now - r["ts"] <= MAX_AGE_SECONDS and "fp" in r]
    seen = bool(use_id) and any(r.get("id") == use_id for r in rows)
    seq = [r.get("fp") for r in rows]
    if not seen:
        seq.append(fp)
    if cfg.get("mode") == "window":
        count = sum(1 for x in seq[-cfg["window"]:] if x == fp)
    else:
        count = 0
        for x in reversed(seq[-cfg["window"]:]):
            if x != fp:
                break
            count += 1
    verdict = level(count, cfg)
    if verdict != "deny" and not seen:
        try:
            if os.path.getsize(path) > MAX_FILE_BYTES:
                return count, verdict
        except OSError:
            pass
        row = {"fp": fp, "tool": tool[:40], "ts": round(now, 3)}
        if use_id:
            row["id"] = use_id[:80]
        paths.append_private(path, (json.dumps(
            row, separators=(",", ":")) + "\n").encode("utf-8"))
    return count, verdict


def level(count: int, cfg: dict) -> str:
    """"deny", "warn" or "ok" for ``count`` identical calls."""
    if cfg["deny"] and count >= cfg["deny"]:
        return "deny"
    if cfg["warn"] and count >= cfg["warn"]:
        return "warn"
    return "ok"


def _how_many(count: int, tool: str, cfg: dict) -> str:
    if cfg.get("mode") == "window":
        return (f"the {count}th identical {tool} call among your last "
                f"{cfg['window']} tool calls")
    return f"the {count}th identical {tool} call in a row"


def warn_text(count: int, tool: str, cfg: dict) -> str:
    deny = (f" It will be refused at {cfg['deny']}." if cfg["deny"] else "")
    return (f"shortleash loop guard: this is {_how_many(count, tool, cfg)}."
            f"{deny} If it keeps giving the same result, stop repeating it: "
            f"read the result, change the approach, or stop and tell the "
            f"user what is blocking you. (If you are legitimately waiting "
            f"or re-running a test after a change, carry on.)")


def deny_text(count: int, tool: str, cfg: dict) -> str:
    return (f"shortleash loop guard: this is {_how_many(count, tool, cfg)}, "
            f"which is the limit. It was not run. Do something different "
            f"(change the command, fix the cause of the error) and you can "
            f"come back to this call, or stop and report to the user what is "
            f"blocking you. The limit was set by the user.")


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
