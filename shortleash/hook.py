"""`shortleash hook`: the leash inside an interactive Claude Code session.

`shortleash run` wraps a non-interactive `claude -p`. An interactive session
has no stream to tail -- but Claude Code runs *hooks*: before every tool call
it starts a command (a PreToolUse hook), hands it the event as JSON on stdin,
and lets the command allow, deny or escalate the call to the human.

This module is that command. On every tool call it reads the session's
transcript (``transcript_path`` from the event, plus the subagent transcripts
next to it), counts what the session has spent, and decides:

- over ``--budget`` or ``--max-tokens``: deny the call and stop Claude
  (``continue: false``) -- a hard stop;
- the call would spawn a subagent past ``--max-spawns``: deny that spawn;
- the call repeats a loop (``--max-repeats``, see loops.py): deny it, with
  the reason, so Claude can change course;
- over ``--ask-over`` (a soft limit below the budget): ``ask`` -- Claude
  Code shows its permission prompt and a human approves or refuses each
  further call. This is the human approval gate.
- otherwise: say nothing (exit 0, no output), and the normal permission
  rules apply.

Every deny or ask is appended to ``hook-log.jsonl`` in the state directory
(see paths): session, decision, reason, cost and token figures -- never the
tool's input or the prompt.

Formats relied on (verified against Claude Code 2.1.318 on 2026-11-05, and
the hooks reference at code.claude.com/docs/en/hooks):
- hook input: ``session_id``, ``transcript_path``, ``hook_event_name``,
  ``tool_name``, ``tool_input``, ``tool_use_id``, and ``agent_id`` when the
  hook fires inside a subagent;
- output: ``{"hookSpecificOutput": {"hookEventName": "PreToolUse",
  "permissionDecision": "deny"|"ask", "permissionDecisionReason": ...}}``,
  plus ``{"continue": false, "stopReason": ...}`` to stop Claude;
- transcript: JSON lines; ``"type": "assistant"`` lines carry ``message``
  with ``id``, ``model``, ``usage`` and ``content`` (tool_use blocks), one
  line per content block with the same id and usage (de-duplicated here as
  in the stream); subagents write ``<session>/subagents/agent-<id>.jsonl``.
- ``"type": "cost-state"`` lines with ``totalCostUSD`` were seen in that
  version's transcripts but are NOT documented: when present, the larger of
  that figure and our own estimate is enforced, never the smaller.

Limits, stated: the transcript is written asynchronously and can lag a
little behind the conversation, so a cap is enforced at the next tool call
after it is crossed, not mid-response. A session that makes no tool calls
is never stopped (a hook only runs at tool calls). Prices are the same
dated fallback table as `run` (see pricing).
"""

from __future__ import annotations

import glob
import json
import os
import shlex
import shutil
import sys
import tempfile
import time

from . import events
from . import loops
from . import paths
from . import pricing

#: Most bytes of hook input read from stdin (the event is small; never let
#: a broken pipe of junk eat memory).
MAX_INPUT = 4 * 1024 * 1024
#: Message ids remembered per transcript for de-duplication (the copies of
#: one message are consecutive).
_MESSAGE_WINDOW = 256

HOOK_LOG = "hook-log.jsonl"
_MAX_MODELS = 32


class HookError(Exception):
    """The hook input cannot be used."""


def _empty_usage() -> dict:
    return {"input": 0, "output": 0, "cache_write": 0, "cache_read": 0,
            "cache_write_1h": 0}


class SessionTally:
    """What a session has spent so far, read from its transcript files."""

    def __init__(self):
        self.usage_by_model: dict[str, dict] = {}
        self.spawn_ids: set[str] = set()
        self.reported_cost: float | None = None
        self.calls: dict[str, list[tuple[str, str]]] = {}  # file -> [(id, fp)]
        self.files = 0
        self.bad_lines = 0

    def tokens(self) -> int:
        return sum(sum(u.get(k, 0) for k in events.USAGE_KEYS)
                   for u in self.usage_by_model.values())

    def estimated_cost(self, table) -> float:
        return sum(pricing.estimate_cost(m, u, table)
                   for m, u in self.usage_by_model.items())

    def cost(self, table) -> float:
        """The figure enforced: our estimate, or Claude Code's own recorded
        total when that is larger (never the smaller of the two)."""
        est = self.estimated_cost(table)
        if self.reported_cost is not None:
            return max(est, self.reported_cost)
        return est

    def unpriced_models(self, table) -> list[str]:
        return [m for m, u in self.usage_by_model.items() if any(u.values())
                and pricing.match(m, table)[0] == "default"]

    # -- reading -----------------------------------------------------

    def read_file(self, path: str, main: bool) -> None:
        try:
            f = open(path, encoding="utf-8", errors="replace")
        except OSError:
            return  # not written yet (the transcript is asynchronous)
        self.files += 1
        seen: dict[str, dict] = {}
        order: list[str] = []
        calls: list[list] = []  # [call id, fingerprint, result digest]
        seen_calls: set[str] = set()
        results: dict[str, str] = {}
        with f:
            for line in f:
                if ('"assistant"' not in line and '"cost-state"' not in line
                        and '"tool_result"' not in line):
                    continue  # cheap filter: most lines are none of these
                try:
                    ev = json.loads(line)
                except (ValueError, RecursionError):
                    # a line still being written -- or one nested too deep
                    # to parse: never a crash (a crash would let the call
                    # through)
                    self.bad_lines += 1
                    continue
                if not isinstance(ev, dict):
                    continue
                if ev.get("type") == "cost-state" and main:
                    c = ev.get("totalCostUSD")
                    if (isinstance(c, (int, float)) and not isinstance(c, bool)
                            and c == c and 0 <= c < 1e12):
                        self.reported_cost = float(c)
                    continue
                msg = ev.get("message")
                if not isinstance(msg, dict):
                    continue
                try:
                    if ev.get("type") == "user":
                        results.update(events._tool_results(msg))
                    elif ev.get("type") == "assistant":
                        self._add_message(msg, seen, order, calls, seen_calls)
                except (TypeError, ValueError, OverflowError, RecursionError):
                    self.bad_lines += 1
        for entry in calls:
            entry[2] = results.get(entry[0])
        self.calls[path] = calls

    def _add_message(self, msg, seen, order, calls, seen_calls) -> None:
        usage = events._usage_of(msg)
        u = {k: max(0, v) for k, v in usage.items()}
        u.setdefault("cache_write_1h", 0)
        mid = str(msg.get("id") or "")
        prev = seen.get(mid) if mid else None
        model = str(msg.get("model") or "")[:200]
        model = prev["__key"] if prev else pricing.price_key(model, u)
        if (model not in self.usage_by_model
                and len(self.usage_by_model) >= _MAX_MODELS):
            model = ""  # bounded: an unknown model is priced at the default
        per = self.usage_by_model.setdefault(model, _empty_usage())
        if prev is not None:
            for k in u:
                per[k] += max(0, u[k] - prev[k])
                prev[k] = max(prev[k], u[k])
        else:
            for k in u:
                per[k] += u[k]
            if mid:
                seen[mid] = dict(u, __key=model)
                order.append(mid)
                if len(order) > _MESSAGE_WINDOW:
                    seen.pop(order.pop(0), None)
        for sid in events._spawn_blocks(msg):
            self.spawn_ids.add(f"{mid}:{sid}" if sid.startswith("<no-id>")
                               else sid)
        for cid, fp in events._tool_calls(msg):
            key = f"{mid}:{cid}" if cid.startswith("<no-id>") else cid
            if key not in seen_calls:
                seen_calls.add(key)
                calls.append([cid, fp, None])


def subagent_transcripts(transcript_path: str) -> list[str]:
    """The subagent transcripts of a session: <dir>/<session>/subagents/
    agent-*.jsonl next to <dir>/<session>.jsonl."""
    stem, ext = os.path.splitext(transcript_path)
    if ext.lower() != ".jsonl":
        return []
    pattern = os.path.join(glob.escape(stem), "subagents", "agent-*.jsonl")
    return sorted(glob.glob(pattern))


def main_transcript(transcript_path: str) -> str:
    """The session's main transcript. The hooks reference does not say
    whether a hook fired inside a subagent gets the session's transcript or
    the subagent's own (<dir>/<session>/subagents/agent-<id>.jsonl); given
    the latter, the main one is <dir>/<session>.jsonl, so the WHOLE
    session's spend is counted either way."""
    parent = os.path.dirname(transcript_path)
    if os.path.basename(parent).lower() == "subagents":
        session_dir = os.path.dirname(parent)
        candidate = session_dir + ".jsonl"
        if os.path.isfile(candidate):
            return candidate
    return transcript_path


def tally_session(transcript_path: str) -> SessionTally:
    transcript_path = main_transcript(transcript_path)
    t = SessionTally()
    t.read_file(transcript_path, main=True)
    for p in subagent_transcripts(transcript_path):
        t.read_file(p, main=False)
    return t


# -- deciding --------------------------------------------------------


def decide(event: dict, tally: SessionTally, *, budget=None, ask_over=None,
           max_tokens=None, max_spawns=None, max_repeats=None,
           table=None) -> dict | None:
    """The decision for one PreToolUse event: None to stay out of the way,
    else {"decision": "deny"|"ask", "reason": str, "stop": bool}."""
    table = table if table is not None else pricing.PricingTable(
        rates=dict(pricing.DEFAULT_PRICING))
    cost = tally.cost(table)
    tokens = tally.tokens()
    tool = str(event.get("tool_name") or "")
    how = ("Claude Code's own recorded total"
           if tally.reported_cost is not None
           and tally.reported_cost >= tally.estimated_cost(table)
           else "estimated from the transcript")
    if budget is not None and cost > budget:
        return {"decision": "deny", "stop": True,
                "reason": (f"shortleash: budget exceeded: ${cost:.4f} spent "
                           f"this session ({how}) > ${budget:.2f} cap. "
                           f"Stop and summarise; raise --budget in the hook "
                           f"settings to continue.")}
    if max_tokens is not None and tokens > max_tokens:
        return {"decision": "deny", "stop": True,
                "reason": (f"shortleash: token cap exceeded: {tokens:,} "
                           f"tokens this session > {max_tokens:,} max.")}
    if max_spawns is not None and tool in events.SPAWN_TOOL_NAMES:
        spawns = len(tally.spawn_ids)
        if str(event.get("tool_use_id") or "") not in tally.spawn_ids:
            spawns += 1  # this call, not in the transcript yet
        if spawns > max_spawns:
            return {"decision": "deny", "stop": False,
                    "reason": (f"shortleash: spawn cap: this would be "
                               f"subagent {spawns} of at most {max_spawns}. "
                               f"Do the work without another subagent.")}
    if max_repeats is not None:
        calls = _calls_for(event, tally)
        tid = str(event.get("tool_use_id") or "")
        ids = [c[0] for c in calls]
        if tid and tid in ids:
            # already in the transcript (it can be, or lag): judge the
            # sequence up to and including this call, not what follows it
            calls = calls[:len(ids) - ids[::-1].index(tid)]
            calls[-1] = [calls[-1][0], calls[-1][1], None]  # not run yet
        else:
            calls = calls + [[tid, loops.fingerprint(
                event.get("tool_name"), event.get("tool_input")), None]]
        block = loops.find_loop([(fp, res) for _, fp, res in calls],
                                max_repeats)
        if block:
            return {"decision": "deny", "stop": False,
                    "reason": (f"shortleash: {loops.describe(block, max_repeats)}"
                               f" -- the agent looks stuck. Try a different "
                               f"approach instead of repeating the call.")}
    if ask_over is not None and cost > ask_over:
        return {"decision": "ask", "stop": False,
                "reason": (f"shortleash: ${cost:.4f} spent this session "
                           f"({how}), over the ${ask_over:.2f} approval "
                           f"threshold"
                           + (f" (hard cap ${budget:.2f})" if budget is not None
                              else "")
                           + ". Approve this tool call to continue.")}
    return None


def _calls_for(event: dict, tally: SessionTally) -> list:
    """The call sequence of the agent this event belongs to: the subagent's
    own transcript when the hook fires inside one, else the main one."""
    transcript = main_transcript(str(event.get("transcript_path") or ""))
    agent_id = event.get("agent_id")
    if agent_id:
        stem = os.path.splitext(transcript)[0]
        want = os.path.join(stem, "subagents", f"agent-{agent_id}.jsonl")
        for path, calls in tally.calls.items():
            if os.path.normcase(path) == os.path.normcase(want):
                return list(calls)
        return []
    return list(tally.calls.get(transcript, []))


def render(decision: dict) -> dict:
    """The JSON Claude Code reads from a PreToolUse hook's stdout."""
    out = {"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": decision["decision"],
        "permissionDecisionReason": decision["reason"],
    }}
    if decision.get("stop"):
        out["continue"] = False
        out["stopReason"] = decision["reason"]
    return out


def _log(event: dict, decision: dict, tally: SessionTally, table) -> None:
    """One line per deny/ask in hook-log.jsonl: the hook's receipt. Never
    the tool input or the prompt. Best effort: a log that cannot be written
    must not change the decision."""
    entry = {
        "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "session_id": str(event.get("session_id") or "")[:200],
        "tool": str(event.get("tool_name") or "")[:200],
        "decision": decision["decision"],
        "stop": bool(decision.get("stop")),
        "reason": decision["reason"],
        "cost_usd": round(tally.cost(table), 6),
        "tokens": tally.tokens(),
        "subagent_spawns": len(tally.spawn_ids),
    }
    try:
        os.makedirs(paths.state_dir(), exist_ok=True)
        with open(os.path.join(paths.state_dir(), HOOK_LOG), "a",
                  encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")
    except OSError:
        pass


def _first_time(event: dict, notes: list[str]) -> bool:
    """True the first time these notes are seen for this session (a marker
    file in the state directory), so a warning is shown once, not on every
    tool call. Without a usable state directory: always True."""
    import hashlib
    key = hashlib.sha256((str(event.get("session_id") or "") + "\0"
                          + "\0".join(notes)).encode("utf-8", "replace")
                         ).hexdigest()[:24]
    marker_dir = os.path.join(paths.state_dir(), "hook-notes")
    marker = os.path.join(marker_dir, key)
    try:
        if os.path.exists(marker):
            return False
        os.makedirs(marker_dir, exist_ok=True)
        with open(marker, "w", encoding="utf-8"):
            pass
    except OSError:
        pass
    return True


def read_event(stream) -> dict:
    raw = stream.read(MAX_INPUT + 1)
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", "replace")
    if len(raw) > MAX_INPUT:
        raise HookError("hook input larger than 4 MB")
    try:
        event = json.loads(raw)
    except RecursionError:
        raise HookError("hook input is nested too deeply") from None
    except ValueError as e:
        raise HookError(f"hook input is not JSON ({e})") from None
    if not isinstance(event, dict):
        raise HookError("hook input is not a JSON object")
    for key in ("tool_name", "tool_use_id", "agent_id", "session_id"):
        if event.get(key) is not None and not isinstance(event[key], str):
            event[key] = str(event[key])  # never trust a field's type
    return event


def run_hook(args, stdin=None, stdout=None, stderr=None) -> int:
    """The `shortleash hook` command. Exit 0 with or without a decision on
    stdout; on unusable input exit 1 (Claude Code shows a non-blocking
    error and the call goes ahead) or, with --fail-closed, exit 2 (the call
    is blocked, the message is the reason)."""
    stdin = stdin or sys.stdin
    stdout = stdout or sys.stdout
    stderr = stderr or sys.stderr
    try:
        return _run_hook(args, stdin, stdout, stderr)
    except Exception as e:  # anything at all: never a traceback, and under
        # --fail-closed never a pass (exit 1 would let the call through)
        try:
            stderr.write(f"shortleash hook: internal error: "
                         f"{type(e).__name__}: {e}\n")
        except Exception:
            pass
        return 2 if getattr(args, "fail_closed", False) else 1


def _run_hook(args, stdin, stdout, stderr) -> int:
    notes: list[str] = []
    try:
        table = pricing.load_table(args.pricing)
    except (OSError, ValueError) as e:
        # A pricing file that cannot be loaded must not switch the caps
        # off: price with the built-in table and say so.
        table = pricing.PricingTable(rates=dict(pricing.DEFAULT_PRICING))
        notes.append(f"pricing file unusable ({e}); the built-in table was "
                     f"used")
    stale = pricing.stale_note(table.as_of)
    if stale:
        notes.append(stale)
    try:
        event = read_event(stdin)
        name = event.get("hook_event_name")
        if name not in (None, "PreToolUse"):
            return 0  # registered on another event by mistake: stay out
        transcript = event.get("transcript_path")
        if not isinstance(transcript, str) or not transcript:
            raise HookError("hook input has no transcript_path")
        tally = tally_session(transcript)
    except (HookError, OSError, ValueError, RecursionError) as e:
        stderr.write(f"shortleash hook: {e}\n")
        return 2 if args.fail_closed else 1
    decision = decide(event, tally, budget=args.budget, ask_over=args.ask_over,
                      max_tokens=args.max_tokens, max_spawns=args.max_spawns,
                      max_repeats=args.max_repeats, table=table)
    if decision is None:
        if notes and _first_time(event, notes):
            # exit 0 with a systemMessage: shown to the user, once per
            # session, and the call goes ahead as it would have
            stdout.write(json.dumps({"systemMessage": "shortleash: "
                                     + "; ".join(notes)}) + "\n")
            stdout.flush()
        return 0
    if notes:
        decision["reason"] += " (" + "; ".join(notes) + ")"
    unpriced = tally.unpriced_models(table)
    if unpriced and decision["decision"] == "deny" and "budget" in decision["reason"]:
        decision["reason"] += (f" (models not in the pricing table, priced at "
                               f"the fallback rate: {', '.join(unpriced)})")
    _log(event, decision, tally, table)
    stdout.write(json.dumps(render(decision)) + "\n")
    stdout.flush()
    return 0


# -- install -----------------------------------------------------------


def hook_command(args, executable: str | None = None) -> str:
    """The command line Claude Code should run for the hook."""
    exe = shutil.which("shortleash") if executable is None else executable
    if exe:
        parts = [exe, "hook"]
    else:
        parts = [sys.executable, "-m", "shortleash", "hook"]
    for flag, value in (("--budget", args.budget), ("--ask-over", args.ask_over),
                        ("--max-tokens", args.max_tokens),
                        ("--max-spawns", args.max_spawns),
                        ("--max-repeats", args.max_repeats),
                        ("--pricing", os.path.abspath(args.pricing)
                         if args.pricing else None)):
        if value is not None:
            parts += [flag, f"{value:g}" if isinstance(value, float)
                      else str(value)]
    if args.fail_closed:
        parts.append("--fail-closed")
    if os.name == "nt":
        import subprocess
        return subprocess.list2cmdline(parts)
    return shlex.join(parts)


def settings_block(command: str, fail_closed: bool = False) -> dict:
    """The settings JSON for the hook. With --fail-closed it also sets
    ``"onFailure": "block"`` (Claude Code 2.1.295+): a hook that cannot
    start, crashes or times out then blocks the call instead of letting it
    through -- --fail-closed alone only covers failures the hook itself
    sees -- and a 60-second timeout instead of the default 600."""
    hook = {"type": "command", "command": command}
    if fail_closed:
        hook["onFailure"] = "block"
        hook["timeout"] = 60
    return {"hooks": {"PreToolUse": [{"matcher": "*", "hooks": [hook]}]}}


def _is_ours(entry) -> bool:
    if not isinstance(entry, dict):
        return False
    for h in entry.get("hooks") or []:
        cmd = h.get("command") if isinstance(h, dict) else None
        if isinstance(cmd, str) and "shortleash" in cmd and " hook" in cmd:
            return True
    return False


def merge_settings(settings: dict, command: str,
                   fail_closed: bool = False) -> dict:
    """``settings`` with our PreToolUse hook in it: an earlier shortleash
    hook entry is replaced, every other hook is kept as it was."""
    if not isinstance(settings, dict):
        raise ValueError("the settings file is not a JSON object")
    out = dict(settings)
    hooks = out.get("hooks", {})
    if not isinstance(hooks, dict):
        raise ValueError('"hooks" in the settings file is not an object')
    hooks = dict(hooks)
    pre = hooks.get("PreToolUse", [])
    if not isinstance(pre, list):
        raise ValueError('"hooks.PreToolUse" in the settings file is not a list')
    pre = [e for e in pre if not _is_ours(e)]
    pre.append(settings_block(command, fail_closed)["hooks"]["PreToolUse"][0])
    hooks["PreToolUse"] = pre
    out["hooks"] = hooks
    return out


def write_settings(path: str, command: str,
                   fail_closed: bool = False) -> str | None:
    """Merge the hook into the settings file at ``path`` (created when
    missing). The previous file is kept as ``<path>.bak``; the write is
    atomic. Returns the backup's path, or None when there was no file."""
    settings: dict = {}
    backup = None
    if os.path.exists(path):
        with open(path, "rb") as f:
            raw = f.read()
        text = raw.decode("utf-8-sig") if raw.strip() else "{}"
        try:
            settings = json.loads(text)
        except ValueError as e:
            raise ValueError(f"{path} is not valid JSON ({e}); "
                             f"not touching it") from None
        backup = path + ".bak"
        shutil.copyfile(path, backup)
    merged = merge_settings(settings, command, fail_closed)
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".settings-", suffix=".json",
                               dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(merged, f, indent=2)
            f.write("\n")
        os.replace(tmp, path)
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise
    return backup
