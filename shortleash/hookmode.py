"""Claude Code hook mode: a spawn limit that works inside a session.

`shortleash run` wraps ONE command and reads its output stream. Hooks are the
other way in: Claude Code itself calls shortleash before it starts a
subagent, so the limit also holds for an interactive session you did not
start through `shortleash run`.

    shortleash hook install [--max-spawns N]   # merge two hooks into settings
    shortleash hook uninstall                  # take exactly those out again
    shortleash hook status                     # what each session has started

What is installed (the whole schema is in hook_entries(); checked against
https://code.claude.com/docs/en/hooks, Oct 2026):

  PreToolUse  matcher "Agent|Task"  -> `shortleash hook pretool`
      Runs before Claude starts a subagent. It counts the session's spawns
      (by session_id) and, past the limit, answers with permissionDecision
      "deny" and a reason Claude reads. THIS is what stops a spawn.
  SubagentStart                     -> `shortleash hook subagent-start`
      Runs when a subagent actually starts. The docs say it cannot block, so
      it only records which agent type started (for `hook status`).

What it can NOT do, plainly:
  * It cannot stop a subagent that is already running, or take back tokens
    already spent. It stops the NEXT start.
  * It sees spawns, not money: there is no budget or token cap here. Use
    `shortleash run` for those.
  * It counts one session_id at a time. A new session starts at zero.
  * A resumed subagent or an agent-team message is not an Agent tool call,
    so it is not counted (SubagentStart sees it; the limit does not apply).
  * If the hook cannot run (the command was moved, python is missing, the
    state folder is not writable) Claude Code carries on and nothing is
    limited. The hook fails OPEN on purpose: a broken guard must never
    break your session. `hook status` shows whether it has been counting.
  * Anyone who can edit your Claude Code settings can remove it.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import shlex
import shutil
import sys
import sysconfig
import time

from . import paths
from . import profile as profile_mod

HOOK_TIMEOUT_SECONDS = 10
#: Subagent tool names: "Agent" in current Claude Code, "Task" in older ones.
SPAWN_MATCHER = "Agent|Task"
SPAWN_TOOLS = ("Agent", "Task")
#: Most state lines read per session (a session with more is not plausible).
MAX_LINES = 100_000
#: State files older than this many days are removed by `hook status`.
KEEP_DAYS = 7
CAP_ENV = "SHORTLEASH_MAX_SPAWNS"
SETTINGS_ENV = "CLAUDE_CONFIG_DIR"


class HookError(Exception):
    """A problem to report in one line (exit 2)."""


# ---------------------------------------------------------------------------
# The Claude Code settings schema, in one place
# ---------------------------------------------------------------------------

def hook_entries(base: str) -> dict:
    """{event: matcher group} for Claude Code's settings.json `hooks`.
    ``base`` is the command prefix (the console script, quoted). Shape per
    the hooks reference: event -> [{"matcher", "hooks": [{"type": "command",
    "command", "timeout" (seconds)}]}]; SubagentStart's matcher is the agent
    type, so "" means every agent."""
    def group(matcher: str, arg: str) -> dict:
        return {"matcher": matcher,
                "hooks": [{"type": "command", "command": f"{base} hook {arg}",
                           "timeout": HOOK_TIMEOUT_SECONDS}]}
    return {"PreToolUse": group(SPAWN_MATCHER, "pretool"),
            "SubagentStart": group("", "subagent-start")}


#: event -> the arguments after "shortleash" that identify our command
_OUR_ARGS = {"PreToolUse": ["hook", "pretool"],
             "SubagentStart": ["hook", "subagent-start"]}


# ---------------------------------------------------------------------------
# The command a hook runs
# ---------------------------------------------------------------------------

def console_script() -> str | None:
    """Absolute path of the installed `shortleash` command, or None (a run
    from a source checkout)."""
    name = "shortleash.exe" if os.name == "nt" else "shortleash"
    dirs = []
    try:
        dirs.append(sysconfig.get_path("scripts"))
        dirs.append(sysconfig.get_path(
            "scripts", sysconfig.get_preferred_scheme("user")))
    except (KeyError, ValueError, AttributeError):
        pass
    for d in dirs:
        if d:
            path = os.path.join(d, name)
            if os.path.isfile(path) and os.access(path, os.X_OK):
                return os.path.abspath(path)
    return None


def self_command() -> str:
    """What a hook shell runs, quoted: the absolute path of the console
    script. Hook shells load no aliases and no venv, and must not run
    `python -m shortleash` from the project folder (Claude Code's working
    directory for hooks puts it first on sys.path, so a cloned repo with its
    own shortleash/ package would run at every tool call). Only when the
    script cannot be found: `python -I -m shortleash` (-I keeps the working
    directory off the import path)."""
    def quote(word: str) -> str:
        return f'"{word}"' if os.name == "nt" else shlex.quote(word)
    script = console_script()
    if script:
        return quote(script)
    return f"{quote(sys.executable)} -I -m shortleash"


def is_ours(event: str, command) -> bool:
    """Did shortleash write this hook command for this event? Exactly that:
    `<path>/shortleash[.exe] hook pretool` or `<python> [-I] -m shortleash
    hook pretool`, nothing that merely mentions it."""
    if not isinstance(command, str) or event not in _OUR_ARGS:
        return False
    try:
        words = shlex.split(command, posix=os.name != "nt")
    except ValueError:
        return False
    args = _OUR_ARGS[event]
    if len(words) <= len(args) or words[-len(args):] != args:
        return False
    head = [w.strip('"') for w in words[:-len(args)]]
    if len(head) == 1:
        return re.split(r"[\\/]", head[0])[-1].lower() in (
            "shortleash", "shortleash.exe")
    return (len(head) in (3, 4) and head[-2:] == ["-m", "shortleash"]
            and (len(head) == 3 or head[1] == "-I"))


# ---------------------------------------------------------------------------
# Merging into settings.json
# ---------------------------------------------------------------------------

def settings_path(scope: str = "user", cwd: str | None = None) -> str:
    """user: ~/.claude/settings.json; project: ./.claude/settings.json;
    local: ./.claude/settings.local.json."""
    if scope == "user":
        base = os.environ.get(SETTINGS_ENV) or os.path.join(
            os.path.expanduser("~"), ".claude")
        return os.path.join(os.path.abspath(os.path.expanduser(base)),
                            "settings.json")
    root = os.path.abspath(cwd or os.getcwd())
    name = "settings.json" if scope == "project" else "settings.local.json"
    return os.path.join(root, ".claude", name)


def _hooks_of(settings: dict, create: bool):
    hooks = settings.get("hooks")
    if hooks is None:
        if not create:
            return None
        hooks = settings["hooks"] = {}
    elif not isinstance(hooks, dict):
        raise HookError('"hooks" in the settings file is not an object')
    return hooks


def merge_hooks(settings: dict, base: str | None = None) -> dict:
    """Add our two hooks to a settings dict without touching anything else.
    Per event: a hook shortleash wrote earlier (any interpreter path) is
    rewritten in place; otherwise our matcher group is APPENDED beside the
    user's own. A hook of the user's is never changed or removed, whatever
    it says. Idempotent. HookError for a `hooks` of an unexpected shape."""
    ours = hook_entries(base or self_command())
    hooks = _hooks_of(settings, create=True)
    for event, entry in ours.items():
        existing = hooks.get(event)
        if existing is None:
            existing = hooks[event] = []
        elif not isinstance(existing, list):
            raise HookError(f'"hooks.{event}" in the settings file is not '
                            f'a list')
        command = entry["hooks"][0]["command"]
        found = False
        for group in list(existing):
            if not isinstance(group, dict) or not isinstance(
                    group.get("hooks"), list):
                continue
            kept = []
            for h in group["hooks"]:
                if isinstance(h, dict) and is_ours(event, h.get("command")):
                    if found:
                        continue          # a stale duplicate
                    h["command"] = command
                    h.setdefault("timeout", HOOK_TIMEOUT_SECONDS)
                    found = True
                kept.append(h)
            if kept:
                group["hooks"] = kept
            elif group["hooks"]:
                existing.remove(group)    # only our own duplicate was in it
        if not found:
            existing.append(entry)
    return settings


def remove_hooks(settings: dict) -> int:
    """Take exactly our hooks out (nothing else); returns how many. Groups
    and events that this empties are removed, ones the user wrote are not."""
    hooks = _hooks_of(settings, create=False)
    if hooks is None:
        return 0
    removed = 0
    for event in list(_OUR_ARGS):
        groups = hooks.get(event)
        if not isinstance(groups, list):
            continue
        for group in list(groups):
            if not isinstance(group, dict) or not isinstance(
                    group.get("hooks"), list):
                continue
            keep = [h for h in group["hooks"]
                    if not (isinstance(h, dict)
                            and is_ours(event, h.get("command")))]
            removed += len(group["hooks"]) - len(keep)
            if len(keep) != len(group["hooks"]):
                if keep:
                    group["hooks"] = keep
                else:
                    groups.remove(group)
        if not groups:
            del hooks[event]
    if not hooks:
        del settings["hooks"]
    return removed


def _parse_settings(raw: bytes | None, path: str) -> dict:
    if raw is None:
        return {}
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError as e:
        raise HookError(f"{path} is not valid UTF-8 ({e.reason} at byte "
                        f"{e.start}); it was not modified") from None
    if not text.strip():
        return {}
    try:
        settings = json.loads(text)
    except ValueError as e:
        raise HookError(f"{path} is not strict JSON ({e}) - comments and "
                        f"trailing commas are not allowed; it was not "
                        f"modified") from None
    if not isinstance(settings, dict):
        raise HookError(f"{path} does not hold a JSON object; it was not "
                        f"modified")
    return settings


def update_settings(path: str, change) -> bool:
    """Read the settings file, apply ``change(settings)`` and write it back
    only if that changed something. Atomic (temp file, then replace); the
    previous content is kept as <name>.bak; line endings and indentation are
    kept; a symlinked file is written through. A file that is not a strict
    JSON object is refused and left exactly as it was. True = written."""
    real = os.path.realpath(path)
    try:
        with open(real, "rb") as f:
            raw = f.read()
    except FileNotFoundError:
        raw = None
    except OSError as e:
        raise HookError(f"cannot read {path}: {e.strerror or e}") from None
    settings = _parse_settings(raw, path)
    before = json.dumps(settings, sort_keys=True)
    try:
        change(settings)
    except HookError as e:
        raise HookError(f"{path}: {e}; it was not modified") from None
    if raw is not None and json.dumps(settings, sort_keys=True) == before:
        return False
    text = (raw or b"").decode("utf-8-sig")
    indent = re.search(r"^([ \t]+)\S", text, re.M)
    newline = "\r\n" if b"\r\n" in (raw or b"") else "\n"
    data = (json.dumps(settings, indent=indent.group(1) if indent else 2,
                       ensure_ascii=False) + "\n"
            ).replace("\n", newline).encode("utf-8")
    tmp = f"{real}.shortleash-{os.getpid()}.tmp"
    try:
        os.makedirs(os.path.dirname(real) or ".", exist_ok=True)
        if raw is not None and raw.strip():
            with open(real + ".bak", "wb") as f:
                f.write(raw)
        with open(tmp, "wb") as f:
            f.write(data)
        if raw is not None:
            with contextlib.suppress(OSError):
                shutil.copymode(real, tmp)
        os.replace(tmp, real)
    except OSError as e:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise HookError(f"could not write {path}: {e.strerror or e}") from None
    return True


# ---------------------------------------------------------------------------
# The limit
# ---------------------------------------------------------------------------

def hook_config_path() -> str:
    return os.path.join(paths.state_dir(), "hook.json")


def write_default_cap(cap: int | None) -> None:
    """Remember the limit chosen at install time (None = remove it)."""
    path = hook_config_path()
    if cap is None:
        with contextlib.suppress(OSError):
            os.unlink(path)
        return
    paths.private_dir(os.path.dirname(path))
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"max_spawns": int(cap)}, f)


def _read_default_cap() -> int | None:
    try:
        with open(hook_config_path(), encoding="utf-8") as f:
            value = json.load(f).get("max_spawns")
    except (OSError, ValueError, AttributeError):
        return None
    return value if isinstance(value, int) and not isinstance(value, bool) \
        and value >= 0 else None


def resolve_cap(cwd: str | None, environ=None) -> tuple[int | None, str]:
    """(limit, where it came from). First found wins: $SHORTLEASH_MAX_SPAWNS,
    the nearest .shortleash.toml ([hook] max_spawns, else max_spawns), the
    number given to `hook install --max-spawns`. (None, "") = no limit."""
    env = os.environ if environ is None else environ
    raw = (env.get(CAP_ENV) or "").strip()
    if raw:
        try:
            value = int(raw)
            if value >= 0:
                return value, f"${CAP_ENV}"
        except ValueError:
            pass
    found = profile_mod.find(cwd) if cwd else None
    if found:
        try:
            data = profile_mod.load(found)
        except (OSError, profile_mod.ProfileError):
            data = {}
        value = (data.get("hook") or {}).get("max_spawns", data.get("max_spawns"))
        if value is not None:
            return value, found
    value = _read_default_cap()
    if value is not None:
        return value, hook_config_path()
    return None, ""


# ---------------------------------------------------------------------------
# Per-session state: one append-only file per session
# ---------------------------------------------------------------------------

def state_dir() -> str:
    return os.path.join(paths.state_dir(), "hooks")


def session_file(session_id: str) -> str:
    """The state file of a session. The id comes from Claude Code but is
    still not trusted as a file name: anything outside [A-Za-z0-9_-] (or too
    long) is replaced by a hash of it."""
    if re.fullmatch(r"[A-Za-z0-9_-]{1,100}", session_id):
        name = session_id
    else:
        name = "h-" + hashlib.sha256(
            session_id.encode("utf-8", "replace")).hexdigest()[:32]
    return os.path.join(state_dir(), name + ".jsonl")


def _append(path: str, record: dict) -> None:
    paths.private_dir(os.path.dirname(path))     # 0700: session ids are here
    line = (json.dumps(record, separators=(",", ":")) + "\n").encode("utf-8")
    fresh = not os.path.exists(path)
    paths.append_private(path, line)    # one write: whole lines, never halves
    if fresh:
        prune()         # a new session: drop state files older than KEEP_DAYS


def read_state(path: str) -> list[dict]:
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


def spawn_ids(rows: list[dict]) -> list[str]:
    """The distinct spawn requests of a session in the order they were
    recorded (a retried call with the same tool_use_id counts once)."""
    seen: dict[str, None] = {}
    for row in rows:
        if row.get("t") == "pre" and isinstance(row.get("id"), str):
            seen.setdefault(row["id"], None)
    return list(seen)


def decide(session_id: str, tool_use_id: str, cap: int | None) -> tuple[int, bool]:
    """Record one spawn request and decide: (its place in the session's
    count, allowed?). The place is its position in the append order of the
    state file, so two requests made at the same moment cannot both take the
    last free place."""
    path = session_file(session_id)
    key = tool_use_id or f"nouse-{time.time_ns()}"
    _append(path, {"t": "pre", "id": key, "ts": round(time.time(), 3)})
    ids = spawn_ids(read_state(path))
    place = ids.index(key) + 1 if key in ids else len(ids)
    allowed = cap is None or place <= cap
    if not allowed:
        _append(path, {"t": "deny", "id": key})
    return place, allowed


def deny_reason(place: int, cap: int) -> str:
    return (f"shortleash: this session has already started {cap} subagent(s), "
            f"which is the limit (request number {place}). Do not start more "
            f"subagents: do the remaining work yourself in this conversation, "
            f"or report what is left. The limit was set by the user.")


def run_pretool(stdin_text: str, environ=None) -> tuple[str, int]:
    """The PreToolUse hook: (text for stdout, exit code). Fails open: any
    problem is an allow (empty output, exit 0)."""
    try:
        event = json.loads(stdin_text)
        if not isinstance(event, dict) or event.get("tool_name") not in SPAWN_TOOLS:
            return "", 0
        session = event.get("session_id")
        if not isinstance(session, str) or not session:
            return "", 0
        cap, _where = resolve_cap(
            event.get("cwd") if isinstance(event.get("cwd"), str) else None,
            environ)
        use_id = event.get("tool_use_id")
        place, allowed = decide(
            session, use_id if isinstance(use_id, str) else "", cap)
        if allowed:
            return "", 0
        return json.dumps({"hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": deny_reason(place, cap)}}), 0
    except Exception:       # noqa: BLE001 - fail open, never break a session
        return "", 0


def run_subagent_start(stdin_text: str) -> tuple[str, int]:
    """The SubagentStart hook: record the start. It cannot block (per the
    docs) and prints nothing."""
    try:
        event = json.loads(stdin_text)
        session = event.get("session_id") if isinstance(event, dict) else None
        if isinstance(session, str) and session:
            agent = event.get("agent_type")
            _append(session_file(session), {
                "t": "start",
                "agent": agent[:60] if isinstance(agent, str) else "?",
                "id": str(event.get("agent_id") or "")[:80],
                "ts": round(time.time(), 3)})
    except Exception:       # noqa: BLE001
        pass
    return "", 0


# ---------------------------------------------------------------------------
# status
# ---------------------------------------------------------------------------

def sessions(limit: int = 10) -> list[dict]:
    """The most recently used sessions: id, spawn requests, denied, agent
    types started, last time."""
    folder = state_dir()
    try:
        names = [n for n in os.listdir(folder) if n.endswith(".jsonl")]
    except OSError:
        return []
    found = []
    for name in names:
        path = os.path.join(folder, name)
        try:
            mtime = os.path.getmtime(path)
        except OSError:
            continue
        rows = read_state(path)
        agents: dict[str, int] = {}
        for row in rows:
            if row.get("t") == "start":
                key = str(row.get("agent") or "?")
                agents[key] = agents.get(key, 0) + 1
        found.append({
            "session": name[:-len(".jsonl")],
            "requests": len(spawn_ids(rows)),
            "denied": sum(1 for r in rows if r.get("t") == "deny"),
            "started": sum(agents.values()),
            "agents": agents, "last": mtime})
    found.sort(key=lambda s: -s["last"])
    return found[:limit]


def prune(days: int = KEEP_DAYS) -> int:
    """Remove state files not touched for ``days`` days; returns how many."""
    folder = state_dir()
    cutoff = time.time() - days * 86400
    removed = 0
    try:
        names = os.listdir(folder)
    except OSError:
        return 0
    for name in names:
        path = os.path.join(folder, name)
        try:
            if name.endswith(".jsonl") and os.path.getmtime(path) < cutoff:
                os.unlink(path)
                removed += 1
        except OSError:
            pass
    return removed


def installed_in(path: str) -> list[str]:
    """Which of our events are in the settings file at ``path``."""
    try:
        with open(path, "rb") as f:
            settings = _parse_settings(f.read(), path)
    except (OSError, HookError):
        return []
    hooks = settings.get("hooks")
    out = []
    if isinstance(hooks, dict):
        for event in _OUR_ARGS:
            for group in hooks.get(event) or []:
                if isinstance(group, dict) and any(
                        isinstance(h, dict) and is_ours(event, h.get("command"))
                        for h in group.get("hooks") or []):
                    out.append(event)
                    break
    return out
