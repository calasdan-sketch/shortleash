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
from . import ledger
from . import loops
from . import paths
from . import pricing

#: Most bytes of hook input read from stdin (the event is small; never let
#: a broken pipe of junk eat memory).
MAX_INPUT = 4 * 1024 * 1024
#: Message ids remembered per transcript for de-duplication (the copies of
#: one message are consecutive).
_MESSAGE_WINDOW = 256
#: Recent call keys remembered per transcript for de-duplication.
_CALL_WINDOW = 1024
#: A transcript line longer than this is skipped (counted as bad) rather
#: than held in memory: one tool result can be huge, but never this huge.
_MAX_LINE = 32 * 1024 * 1024

HOOK_LOG = "hook-log.jsonl"
_MAX_MODELS = 32


class HookError(Exception):
    """The hook input cannot be used."""


def _empty_usage() -> dict:
    return {"input": 0, "output": 0, "cache_write": 0, "cache_read": 0,
            "cache_write_1h": 0}


class FileState:
    """What one transcript file has told us so far, resumable.

    A transcript only ever grows, so a hook call need not re-read the whole
    of it: the state after the first ``offset`` bytes is kept (see
    ``hookcache``) and only the new lines are fed in. The same code reads a
    file from scratch (offset 0), so there is one way to count, not two."""

    def __init__(self, fuzzy: bool = False):
        self.fuzzy = fuzzy                 # --loop-fuzzy fingerprints
        self.err_streak = 0                # tool errors in a row, up to now
        self.usage_by_model: dict[str, dict] = {}
        self.spawn_ids: set[str] = set()
        self.reported_cost: float | None = None
        self.calls: list[list] = []        # [call id, fingerprint, result]
        self.bad_lines = 0
        self.offset = 0                    # bytes fed so far (whole lines)
        self.seen: dict[str, dict] = {}    # message id -> usage counted
        self.order: list[str] = []         # ids in `seen`, oldest first
        self.seen_calls: list[str] = []    # recent call keys (de-dupe)
        self._seen_calls_set: set[str] = set()
        self._by_id: dict[str, list] = {}  # call id -> its entry in calls

    # -- feeding -----------------------------------------------------

    def feed_line(self, line: str, main: bool) -> None:
        if ('"assistant"' not in line and '"cost-state"' not in line
                and '"tool_result"' not in line):
            return  # cheap filter: most lines are none of these
        try:
            ev = json.loads(line)
        except (ValueError, RecursionError):
            # a line still being written -- or one nested too deep to
            # parse: never a crash (a crash would let the call through)
            self.bad_lines += 1
            return
        if not isinstance(ev, dict):
            return
        if ev.get("type") == "cost-state" and main:
            c = ev.get("totalCostUSD")
            if (isinstance(c, (int, float)) and not isinstance(c, bool)
                    and c == c and 0 <= c < 1e12):
                self.reported_cost = float(c)
            return
        msg = ev.get("message")
        if not isinstance(msg, dict):
            return
        try:
            if ev.get("type") == "user":
                for cid, digest in events._tool_results(msg, self.fuzzy):
                    entry = self._by_id.get(cid)
                    if entry is not None:
                        entry[2] = digest
                for failed in events._tool_result_errors(msg):
                    self.err_streak = self.err_streak + 1 if failed else 0
            elif ev.get("type") == "assistant":
                self._add_message(msg)
        except (TypeError, ValueError, OverflowError, RecursionError):
            self.bad_lines += 1

    def _add_message(self, msg) -> None:
        usage = events._usage_of(msg)
        u = {k: max(0, v) for k, v in usage.items()}
        u.setdefault("cache_write_1h", 0)
        mid = str(msg.get("id") or "")
        prev = self.seen.get(mid) if mid else None
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
                self.seen[mid] = dict(u, __key=model)
                self.order.append(mid)
                if len(self.order) > _MESSAGE_WINDOW:
                    self.seen.pop(self.order.pop(0), None)
        for sid in events._spawn_blocks(msg):
            self.spawn_ids.add(f"{mid}:{sid}" if sid.startswith("<no-id>")
                               else sid)
        for cid, fp in events._tool_calls(msg, self.fuzzy):
            key = f"{mid}:{cid}" if cid.startswith("<no-id>") else cid
            if key in self._seen_calls_set:
                continue
            self._seen_calls_set.add(key)
            self.seen_calls.append(key)
            if len(self.seen_calls) > _CALL_WINDOW:
                self._seen_calls_set.discard(self.seen_calls.pop(0))
            entry = [cid, fp, None]
            self.calls.append(entry)
            self._by_id[cid] = entry
            if len(self.calls) > loops.HISTORY:
                old = self.calls.pop(0)
                if self._by_id.get(old[0]) is old:
                    del self._by_id[old[0]]

    # -- (de)serialising for the cache ---------------------------------

    def to_dict(self) -> dict:
        return {"fuzzy": self.fuzzy, "err_streak": self.err_streak,
                "usage_by_model": self.usage_by_model,
                "spawn_ids": sorted(self.spawn_ids),
                "reported_cost": self.reported_cost,
                "calls": self.calls, "bad_lines": self.bad_lines,
                "offset": self.offset, "seen": self.seen,
                "order": self.order, "seen_calls": self.seen_calls}

    @classmethod
    def from_dict(cls, d: dict) -> "FileState":
        """Raises (ValueError/TypeError/KeyError) on anything not shaped
        like what to_dict wrote: the caller then starts from scratch."""
        st = cls(bool(d["fuzzy"]))
        st.err_streak = max(0, int(d["err_streak"]))
        st.usage_by_model = {str(m): {str(k): int(v) for k, v in u.items()}
                             for m, u in d["usage_by_model"].items()}
        st.spawn_ids = {str(x) for x in d["spawn_ids"]}
        rc = d["reported_cost"]
        st.reported_cost = None if rc is None else float(rc)
        st.calls = [[str(c[0]), str(c[1]), None if c[2] is None else str(c[2])]
                    for c in d["calls"]]
        st.bad_lines = int(d["bad_lines"])
        st.offset = int(d["offset"])
        st.seen = {str(m): {str(k): (v if k == "__key" else int(v))
                            for k, v in u.items()}
                   for m, u in d["seen"].items()}
        st.order = [str(x) for x in d["order"]]
        st.seen_calls = [str(x) for x in d["seen_calls"]]
        if (st.offset < 0 or len(st.calls) > loops.HISTORY
                or len(st.seen) > _MESSAGE_WINDOW + 1
                or len(st.usage_by_model) > _MAX_MODELS + 1
                or len(st.order) > _MESSAGE_WINDOW + 1
                or len(st.seen_calls) > _CALL_WINDOW + 1):
            raise ValueError("cached state out of bounds")
        st._seen_calls_set = set(st.seen_calls)
        st._by_id = {c[0]: c for c in st.calls}
        return st

    def clone(self) -> "FileState":
        return FileState.from_dict(json.loads(json.dumps(self.to_dict())))


def advance(path: str, main: bool, state: "FileState | None" = None,
            fuzzy: bool = False) -> "tuple[FileState, FileState, bool] | None":
    """Feed the new complete lines of ``path`` into ``state`` (a fresh one
    when None). Returns (persistable state, effective state, changed) or
    None when the file cannot be opened.

    The persistable state stops at the last newline. A final line without
    one may be a half-written line or a complete one whose newline has not
    landed: it is counted in the *effective* state (a copy) but not
    persisted, so it is never counted twice."""
    state = state or FileState(fuzzy)
    try:
        f = open(path, "rb")
    except OSError:
        return None
    changed = False
    with f:
        try:
            size = os.fstat(f.fileno()).st_size
        except OSError:
            size = 0
        if size < state.offset:  # truncated or replaced: start over
            state = FileState(fuzzy)
        f.seek(state.offset)
        tail = b""
        while True:
            raw = f.readline(_MAX_LINE + 1)
            if not raw:
                break
            if len(raw) > _MAX_LINE:
                # absurdly long line: skip it without holding it
                skipped = len(raw)
                while not raw.endswith(b"\n"):
                    raw = f.readline(1 << 20)
                    if not raw:
                        break
                    skipped += len(raw)
                if not raw:  # the line is still being written
                    break
                state.offset += skipped
                state.bad_lines += 1
                changed = True
                continue
            if not raw.endswith(b"\n"):
                tail = raw
                break
            state.offset += len(raw)
            changed = True
            state.feed_line(raw.decode("utf-8", "replace"), main)
    effective = state
    if tail.strip():
        effective = state.clone()
        effective.feed_line(tail.decode("utf-8", "replace"), main)
    return state, effective, changed


class SessionTally:
    """What a session has spent so far, read from its transcript files."""

    def __init__(self):
        self.usage_by_model: dict[str, dict] = {}
        self.spawn_ids: set[str] = set()
        self.reported_cost: float | None = None
        self.calls: dict[str, list[tuple[str, str]]] = {}  # file -> [(id, fp)]
        self.streaks: dict[str, int] = {}  # file -> tool errors in a row
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

    def add_state(self, path: str, st: FileState) -> None:
        self.files += 1
        self.bad_lines += st.bad_lines
        self.spawn_ids |= st.spawn_ids
        if st.reported_cost is not None:
            self.reported_cost = st.reported_cost
        for model, u in st.usage_by_model.items():
            if (model not in self.usage_by_model
                    and len(self.usage_by_model) >= _MAX_MODELS):
                model = ""
            per = self.usage_by_model.setdefault(model, _empty_usage())
            for k, v in u.items():
                per[k] = per.get(k, 0) + v
        self.calls[path] = st.calls  # shared, never mutated afterwards
        self.streaks[path] = st.err_streak

    def read_file(self, path: str, main: bool, fuzzy: bool = False) -> None:
        got = advance(path, main, None, fuzzy)
        if got is not None:  # None: not written yet (asynchronous)
            self.add_state(path, got[1])


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


# -- the cache: a session is not re-read from byte 0 on every tool call --

CACHE_DIR = "hook-cache"
CACHE_VERSION = 1
_HEAD = 256            # bytes of a file's start kept to notice a replacement
_CACHE_KEEP_DAYS = 14


def _cache_dir(main_path: str, fuzzy: bool = False) -> str:
    import hashlib
    key = os.path.normcase(os.path.abspath(main_path)) + ("\0fuzzy" if fuzzy
                                                         else "")
    return os.path.join(paths.state_dir(), CACHE_DIR,
                        hashlib.sha256(key.encode("utf-8", "replace"))
                        .hexdigest()[:32])


def _entry_name(path: str) -> str:
    import hashlib
    return hashlib.sha256(os.path.normcase(os.path.abspath(path))
                          .encode("utf-8", "replace")).hexdigest()[:32] + ".json"


def _head_of(path: str) -> str:
    import hashlib
    try:
        with open(path, "rb") as f:
            return hashlib.sha1(f.read(_HEAD)).hexdigest()
    except OSError:
        return ""


def _load_entry(directory: str, path: str):
    """The cached {"head", "size", "mtime_ns", "state"} of one transcript
    file, or None. Anything wrong with the cache file (missing, corrupt,
    another version, odd values, for another path) is None: that file is
    simply read from its start."""
    try:
        with open(os.path.join(directory, _entry_name(path)),
                  encoding="utf-8") as f:
            e = json.load(f)
        if e.get("v") != CACHE_VERSION or e.get("path") != path:
            return None
        return {"head": str(e["head"]), "size": int(e["size"]),
                "mtime_ns": int(e["mtime_ns"]),
                "state": FileState.from_dict(e["state"])}
    except (OSError, ValueError, TypeError, KeyError, AttributeError,
            RecursionError):
        return None


def _save_entry(directory: str, path: str, e: dict) -> None:
    """Best effort and atomic (a temp file renamed over the old one), so a
    parallel hook or a crash leaves a whole file, never half of one. A
    cache that cannot be written only costs speed. One file per transcript:
    only the file that grew is rewritten."""
    try:
        os.makedirs(directory, exist_ok=True)
        blob = json.dumps({"v": CACHE_VERSION, "path": path,
                           "head": e["head"], "size": e["size"],
                           "mtime_ns": e["mtime_ns"],
                           "state": e["state"].to_dict()},
                          separators=(",", ":"))
        fd, tmp = tempfile.mkstemp(prefix=".c-", suffix=".tmp", dir=directory)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(blob)
            os.replace(tmp, os.path.join(directory, _entry_name(path)))
        except BaseException:
            try:
                os.remove(tmp)
            except OSError:
                pass
            raise
    except (OSError, ValueError, TypeError, RecursionError):
        pass


def _prune_cache(keep: str) -> None:
    """Drop session cache directories nobody has touched for two weeks (a
    few per call at most; the directory is small)."""
    import shutil
    root = os.path.dirname(keep)
    cutoff = time.time() - _CACHE_KEEP_DAYS * 86400
    try:
        names = os.listdir(root)
    except OSError:
        return
    for name in names[:200]:
        full = os.path.join(root, name)
        try:
            if full != keep and os.stat(full).st_mtime < cutoff:
                shutil.rmtree(full, ignore_errors=True)
        except OSError:
            pass


def tally_session(transcript_path: str, use_cache: bool = False,
                  fuzzy: bool = False) -> SessionTally:
    """Count a session. With ``use_cache`` only what was appended since the
    last call is read (the state after the earlier bytes is kept under the
    state directory); the figures are the same as a full read -- a cache
    that is stale, corrupt or for a file that was replaced is dropped."""
    transcript_path = main_transcript(transcript_path)
    t = SessionTally()
    files = [(transcript_path, True)] + [
        (p, False) for p in subagent_transcripts(transcript_path)]
    directory = _cache_dir(transcript_path, fuzzy) if use_cache else ""
    created = use_cache and not os.path.isdir(directory)
    for path, main in files:
        if not use_cache:
            t.read_file(path, main, fuzzy)
            continue
        try:
            stt = os.stat(path)
        except OSError:
            continue  # not written yet
        entry = _load_entry(directory, path)
        if entry is not None and entry["state"].offset > stt.st_size:
            entry = None  # truncated
        if (entry is not None and entry["size"] == stt.st_size
                and entry["mtime_ns"] == stt.st_mtime_ns
                and entry["state"].offset == stt.st_size):
            t.add_state(path, entry["state"])  # nothing new
            continue
        if (entry is not None and entry["state"].offset > 0
                and entry["head"] != _head_of(path)):
            entry = None  # replaced by another file
        got = advance(path, main, entry["state"] if entry else None, fuzzy)
        if got is None:
            continue
        persisted, effective, _ = got
        _save_entry(directory, path, {
            "head": _head_of(path), "size": stt.st_size,
            "mtime_ns": stt.st_mtime_ns, "state": persisted})
        t.add_state(path, effective)
    if created:
        _prune_cache(directory)
    return t


# -- deciding --------------------------------------------------------


def over_cap(tally: SessionTally, table, *, budget=None, max_tokens=None,
             daily_budget=None, day_spend=None, project_budget=None,
             project_spend=None) -> str | None:
    """Why the session may not go on, or None: the caps that end it for
    good (session budget and tokens, the machine's daily and the project's
    daily budget from the ledger)."""
    cost = tally.cost(table)
    how = ("Claude Code's own recorded total"
           if tally.reported_cost is not None
           and tally.reported_cost >= tally.estimated_cost(table)
           else "estimated from the transcript")
    if budget is not None and cost > budget:
        return (f"shortleash: budget exceeded: ${cost:.4f} spent "
                f"this session ({how}) > ${budget:.2f} cap. "
                f"Stop and summarise; raise --budget in the hook "
                f"settings to continue.")
    tokens = tally.tokens()
    if max_tokens is not None and tokens > max_tokens:
        return (f"shortleash: token cap exceeded: {tokens:,} "
                f"tokens this session > {max_tokens:,} max.")
    if (daily_budget is not None and day_spend is not None
            and day_spend > daily_budget):
        return (f"shortleash: daily budget exceeded: ${day_spend:.4f} spent "
                f"today by all sessions on this machine > "
                f"${daily_budget:.2f} cap (this session: ${cost:.4f}). "
                f"Stop and summarise; raise --daily-budget to continue.")
    if (project_budget is not None and project_spend is not None
            and project_spend > project_budget):
        return (f"shortleash: project daily budget exceeded: "
                f"${project_spend:.4f} spent today in this project > "
                f"${project_budget:.2f} cap (this session: ${cost:.4f}). "
                f"Stop and summarise; raise --project-daily-budget to "
                f"continue.")
    return None


def decide(event: dict, tally: SessionTally, *, budget=None, ask_over=None,
           max_tokens=None, max_spawns=None, max_repeats=None,
           table=None, daily_budget=None, day_spend=None,
           project_budget=None, project_spend=None, fuzzy=False,
           max_errors=None) -> dict | None:
    """The decision for one PreToolUse event: None to stay out of the way,
    else {"decision": "deny"|"ask", "reason": str, "stop": bool}."""
    table = table if table is not None else pricing.PricingTable(
        rates=dict(pricing.DEFAULT_PRICING))
    cost = tally.cost(table)
    tool = str(event.get("tool_name") or "")
    how = ("Claude Code's own recorded total"
           if tally.reported_cost is not None
           and tally.reported_cost >= tally.estimated_cost(table)
           else "estimated from the transcript")
    why = over_cap(tally, table, budget=budget, max_tokens=max_tokens,
                   daily_budget=daily_budget, day_spend=day_spend,
                   project_budget=project_budget, project_spend=project_spend)
    if why:
        return {"decision": "deny", "stop": True, "reason": why}
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
                event.get("tool_name"), event.get("tool_input"), fuzzy), None]]
        block = loops.find_loop([(fp, res) for _, fp, res in calls],
                                max_repeats)
        if block:
            return {"decision": "deny", "stop": False,
                    "reason": (f"shortleash: {loops.describe(block, max_repeats)}"
                               f" -- the agent looks stuck. Try a different "
                               f"approach instead of repeating the call.")}
    if max_errors is not None:
        streak = _streak_for(event, tally)
        if streak >= max_errors:
            return {"decision": "deny", "stop": False,
                    "reason": (f"shortleash: {streak} tool calls in a row "
                               f"failed (limit {max_errors}) -- the agent "
                               f"looks stuck. Stop retrying; say what is "
                               f"failing and try a different approach, or "
                               f"ask the user.")}
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


def _streak_for(event: dict, tally: SessionTally) -> int:
    """Tool errors in a row for the agent this event belongs to."""
    transcript = main_transcript(str(event.get("transcript_path") or ""))
    agent_id = event.get("agent_id")
    if agent_id:
        stem = os.path.splitext(transcript)[0]
        transcript = os.path.join(stem, "subagents", f"agent-{agent_id}.jsonl")
    for path, streak in tally.streaks.items():
        if os.path.normcase(path) == os.path.normcase(transcript):
            return streak
    return 0


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
    daily = getattr(args, "daily_budget", None)
    pdaily = getattr(args, "project_daily_budget", None)
    prompts = bool(getattr(args, "block_prompts", False))
    fuzzy = bool(getattr(args, "loop_fuzzy", False))
    try:
        event = read_event(stdin)
        name = event.get("hook_event_name")
        if name not in (None, "PreToolUse") and not (
                prompts and name == "UserPromptSubmit"):
            return 0  # registered on another event by mistake: stay out
        transcript = event.get("transcript_path")
        if not isinstance(transcript, str) or not transcript:
            raise HookError("hook input has no transcript_path")
        tally = tally_session(
            transcript, use_cache=not os.environ.get("SHORTLEASH_NO_CACHE"),
            fuzzy=fuzzy)
    except (HookError, OSError, ValueError, RecursionError) as e:
        stderr.write(f"shortleash hook: {e}\n")
        return 2 if args.fail_closed else 1
    day_spend = project_spend = None
    if daily is not None or pdaily is not None:
        try:
            day_spend, project_spend = _ledger_totals(
                event, tally, table, daily is not None, pdaily is not None)
        except OSError as e:
            # A cap that cannot be checked must not look enforced.
            stderr.write(f"shortleash hook: the spend ledger cannot be "
                         f"used ({e}); --daily-budget / --project-daily-"
                         f"budget were NOT checked\n")
            return 2 if args.fail_closed else 1
    caps = dict(budget=args.budget, max_tokens=args.max_tokens, table=table,
                daily_budget=daily, day_spend=day_spend,
                project_budget=pdaily, project_spend=project_spend)
    if event.get("hook_event_name") == "UserPromptSubmit":
        # A turn without tool calls never reaches PreToolUse: this is the
        # only point where a session that is over its caps is stopped from
        # taking one more prompt.
        why = over_cap(tally, **{k: v for k, v in caps.items()
                                 if k != "table"}, table=table)
        if not why:
            return 0
        decision = {"decision": "block", "stop": False, "reason": why}
        _log(event, decision, tally, table)
        stdout.write(json.dumps({"decision": "block", "reason": why}) + "\n")
        stdout.flush()
        return 0
    decision = decide(event, tally, ask_over=args.ask_over,
                      max_spawns=args.max_spawns,
                      max_repeats=args.max_repeats, fuzzy=fuzzy,
                      max_errors=getattr(args, "max_errors", None), **caps)
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


def _ledger_totals(event: dict, tally: SessionTally, table, want_day: bool,
                   want_project: bool) -> tuple:
    """Record this session's spend in the ledger and return (today's spend
    by every session on the machine, today's spend in this project) -- each
    None when its cap is not set. Raises OSError when the ledger cannot be
    written."""
    transcript = main_transcript(str(event.get("transcript_path") or ""))
    sid = str(event.get("session_id") or "") or transcript
    project = ledger.project_of(transcript)
    ledger.record(sid, tally.cost(table), project)
    day = ledger.today_spend() if want_day else None
    proj = ledger.today_spend(project=project) if want_project else None
    return day, proj


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
                        ("--max-errors", getattr(args, "max_errors", None)),
                        ("--daily-budget", getattr(args, "daily_budget", None)),
                        ("--project-daily-budget",
                         getattr(args, "project_daily_budget", None)),
                        ("--pricing", os.path.abspath(args.pricing)
                         if args.pricing else None)):
        if value is not None:
            parts += [flag, f"{value:g}" if isinstance(value, float)
                      else str(value)]
    if getattr(args, "loop_fuzzy", False):
        parts.append("--loop-fuzzy")
    if getattr(args, "block_prompts", False):
        parts.append("--block-prompts")
    if args.fail_closed:
        parts.append("--fail-closed")
    if os.name == "nt":
        import subprocess
        return subprocess.list2cmdline(parts)
    return shlex.join(parts)


def settings_block(command: str, fail_closed: bool = False,
                   prompts: bool = False) -> dict:
    """The settings JSON for the hook. With --fail-closed it also sets
    ``"onFailure": "block"`` (Claude Code 2.1.295+): a hook that cannot
    start, crashes or times out then blocks the call instead of letting it
    through -- --fail-closed alone only covers failures the hook itself
    sees -- and a 60-second timeout instead of the default 600."""
    hook = {"type": "command", "command": command}
    if fail_closed:
        hook["onFailure"] = "block"
        hook["timeout"] = 60
    out = {"hooks": {"PreToolUse": [{"matcher": "*", "hooks": [hook]}]}}
    if prompts:
        # UserPromptSubmit takes no matcher
        out["hooks"]["UserPromptSubmit"] = [{"hooks": [dict(hook)]}]
    return out


def _is_ours(entry) -> bool:
    if not isinstance(entry, dict):
        return False
    for h in entry.get("hooks") or []:
        cmd = h.get("command") if isinstance(h, dict) else None
        if isinstance(cmd, str) and "shortleash" in cmd and " hook" in cmd:
            return True
    return False


def merge_settings(settings: dict, command: str,
                   fail_closed: bool = False, prompts: bool = False) -> dict:
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
    block = settings_block(command, fail_closed, prompts)["hooks"]
    pre.append(block["PreToolUse"][0])
    hooks["PreToolUse"] = pre
    ups = hooks.get("UserPromptSubmit", [])
    if not isinstance(ups, list):
        raise ValueError('"hooks.UserPromptSubmit" in the settings file is '
                         'not a list')
    ups = [e for e in ups if not _is_ours(e)]  # an earlier install's entry
    if prompts:
        ups.append(block["UserPromptSubmit"][0])
    if ups or "UserPromptSubmit" in hooks:
        hooks["UserPromptSubmit"] = ups
    out["hooks"] = hooks
    return out


def write_settings(path: str, command: str,
                   fail_closed: bool = False, prompts: bool = False
                   ) -> str | None:
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
    merged = merge_settings(settings, command, fail_closed, prompts)
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
