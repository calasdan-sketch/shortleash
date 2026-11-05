"""Loop / stuck detection: the same tool call made over and over.

An agent that is stuck does not always spend fast -- it can retry the same
failing command, or flip between two calls (read a file, run the same test,
read the same file...), for an hour at a few cents a turn. Budget caps catch
that late; a loop cap catches it after N repetitions.

A tool call is reduced to a *fingerprint*: the tool's name and a short hash
of its input (canonical JSON, keys sorted, trailing white space of every
string removed). The input itself is never kept or shown: a Bash command can
carry a token, and the fingerprint ends up in the receipt.

When the call's RESULT is known (the stream's `user` events and the
transcript carry `tool_result` blocks), it is part of the comparison: the
same call is only a repeat if it also got the same result. Running the same
test after each edit is not a loop as long as its output changes. A call
whose result has not arrived yet is compared using the result its last
identical call got ("the same call, which last gave the same answer, again").

A loop is the tail of the sequence being one block of 1 to ``MAX_PERIOD``
calls repeated ``repeats`` times in a row::

    A A A A A          period 1, 5 repeats
    A B A B A B A B    period 2, 4 repeats  (B differs from A)

Polling tools (``POLLING_TOOLS``: reading a background job's output, a task's
status) are made to be called again and again; a block made only of them is
never a loop.

Limits, by design: matching is exact. The same call with a trivially
different input (another timestamp, a shifted line range, an extra space in
the middle) or a result that changes each time (a clock, a PID) is not a
repeat, so an agent that varies its calls evades this cap. It catches
literal retries and short cycles, not every way to go round in circles.
"""

from __future__ import annotations

import hashlib
import json

#: Longest repeating block looked for (A, AB, ABC, ABCD).
MAX_PERIOD = 4
#: How many recent calls are kept to look for a loop in.
HISTORY = 512
#: Tools whose whole point is to be called repeatedly (polling a background
#: shell, a task, a monitor). Claude Code names; matched exactly.
POLLING_TOOLS = frozenset({
    "BashOutput", "TaskOutput", "TaskGet", "TaskList", "Monitor",
    "KillShell", "TodoRead", "ListMcpResourcesTool", "ReadMcpResourceTool",
})


def _normalise(obj, depth: int = 0):
    """Trailing white space off every string (keys kept as they are)."""
    if depth > 200:
        raise RecursionError("too deep")
    if isinstance(obj, str):
        return "\n".join(line.rstrip() for line in obj.rstrip().split("\n"))
    if isinstance(obj, dict):
        return {k: _normalise(v, depth + 1) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_normalise(v, depth + 1) for v in obj]
    return obj


def _digest(obj) -> str:
    try:
        canon = json.dumps(_normalise(obj), sort_keys=True, ensure_ascii=False,
                           separators=(",", ":"), default=str)
    except (TypeError, ValueError, RecursionError):
        canon = repr(type(obj))
    return hashlib.sha256(canon.encode("utf-8", "replace")).hexdigest()[:8]


def fingerprint(name, tool_input) -> str:
    """'<name>#<hash>' for a tool call: equal for the same tool called with
    the same input, whatever the key order. Never raises."""
    name = str(name or "?")[:80]
    return f"{name}#{_digest(tool_input)}"


def result_digest(content, is_error=False) -> str:
    """A short hash of a tool_result's content (and whether it failed)."""
    return _digest([bool(is_error), content])


def tool_name(fp: str) -> str:
    return fp.rsplit("#", 1)[0]


def keys(calls) -> list:
    """Comparison keys for a sequence of (fingerprint, result digest or
    None), oldest first: fingerprint + result, a missing result borrowed
    from the latest earlier identical call."""
    last: dict = {}
    out = []
    for fp, res in calls:
        if res is None:
            res = last.get(fp)
        else:
            last[fp] = res
        out.append(f"{fp}|{res or ''}")
    return out


def find_loop(calls, repeats: int, max_period: int = MAX_PERIOD):
    """The repeating block (of fingerprints) at the end of ``calls`` when
    the last ``repeats`` blocks of some period are identical, else None. The
    shortest period wins. ``calls`` holds fingerprints, or (fingerprint,
    result digest or None) pairs (see keys)."""
    if repeats is None or repeats < 2:
        return None
    calls = [c if isinstance(c, tuple) else (c, None) for c in calls]
    ks = keys(calls)
    for period in range(1, max_period + 1):
        need = period * repeats
        if len(ks) < need:
            break
        tail = ks[-need:]
        block = tail[:period]
        if period > 1 and len(set(block)) == 1:
            continue  # all the same: period 1 is the true answer
        if all(tail[i] == block[i % period] for i in range(need)):
            fps = [k.split("|", 1)[0] for k in block]
            if all(tool_name(fp) in POLLING_TOOLS for fp in fps):
                continue  # polling is supposed to repeat
            return fps
    return None


def describe(block, repeats: int) -> str:
    """Human text for a loop: never the tool input, only names and hashes."""
    if len(block) == 1:
        return (f"loop detected: the same {tool_name(block[0])} call "
                f"(input {block[0].rsplit('#', 1)[-1]}) was made {repeats} "
                f"times in a row")
    names = " -> ".join(f"{tool_name(fp)} ({fp.rsplit('#', 1)[-1]})"
                        for fp in block)
    return (f"loop detected: the same {len(block)} calls ({names}) were "
            f"repeated {repeats} times in a row")
