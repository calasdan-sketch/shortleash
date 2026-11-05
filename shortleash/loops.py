"""Loop / stuck detection: the same tool call made over and over.

An agent that is stuck does not always spend fast -- it can retry the same
failing command, or flip between two calls (read a file, run the same test,
read the same file...), for an hour at a few cents a turn. Budget caps catch
that late; a loop cap catches it after N repetitions.

A tool call is reduced to a *fingerprint*: the tool's name and a short hash
of its input (canonical JSON, keys sorted). The input itself is never kept or
shown: a Bash command can carry a token, and the fingerprint ends up in the
receipt.

A loop is the tail of the call sequence being one block of 1 to
``MAX_PERIOD`` calls repeated ``repeats`` times in a row::

    A A A A A          period 1, 5 repeats
    A B A B A B A B    period 2, 4 repeats  (B differs from A)

Calls with different inputs (an edit, then the test, then a different edit)
never form a loop, so a healthy edit-test cycle is not flagged, however long.
"""

from __future__ import annotations

import hashlib
import json

#: Longest repeating block looked for (A, AB, ABC).
MAX_PERIOD = 3
#: How many recent calls are kept to look for a loop in.
HISTORY = 512


def fingerprint(name, tool_input) -> str:
    """'<name>#<hash>' for a tool call: equal for the same tool called with
    the same input, whatever the key order. Never raises."""
    name = str(name or "?")[:80]
    try:
        canon = json.dumps(tool_input, sort_keys=True, ensure_ascii=False,
                           separators=(",", ":"), default=str)
    except (TypeError, ValueError, RecursionError):
        canon = repr(type(tool_input))
    digest = hashlib.sha256(canon.encode("utf-8", "replace")).hexdigest()[:8]
    return f"{name}#{digest}"


def tool_name(fp: str) -> str:
    return fp.rsplit("#", 1)[0]


def find_loop(calls, repeats: int, max_period: int = MAX_PERIOD):
    """The repeating block at the end of ``calls`` -- a list of
    fingerprints, oldest first -- when the last ``repeats`` blocks of some
    period are identical, else None. The shortest period wins."""
    if repeats is None or repeats < 2:
        return None
    calls = list(calls)
    for period in range(1, max_period + 1):
        need = period * repeats
        if len(calls) < need:
            break
        tail = calls[-need:]
        block = tail[:period]
        if period > 1 and len(set(block)) == 1:
            continue  # all the same: period 1 is the true answer
        if all(tail[i] == block[i % period] for i in range(need)):
            return block
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
