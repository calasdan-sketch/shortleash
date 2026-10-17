"""Resolve the command to launch -- safely on Windows.

Why this exists: on Windows, `claude` installed with npm is a `claude.cmd`
shim, and CreateProcess runs a .cmd/.bat file through cmd.exe. cmd.exe
re-parses the command line, so argument text that is perfectly safe in a
list of arguments gets mangled or interpreted: `%VAR%` is expanded, `^`
is eaten, an embedded `"` ends the quoting and lets `&`, `|`, `<`, `>`
run other commands (the BatBadBut class of argument injection). A prompt
read from a file or a ticket is exactly that kind of text.

Strategy (see resolve_command):

1. Find the executable with shutil.which -- CreateProcess does not look up
   PATHEXT shims, shutil.which does.
2. If it is an npm shim (`claude.cmd`), read the shim and launch what it
   would launch -- `node <...\\node_modules\\@anthropic-ai\\claude-code\\cli.js>`
   -- directly, so cmd.exe is never involved.
3. Any other .cmd/.bat is refused when an argument contains a character
   cmd.exe would interpret (`"`, `%`, `!`, `^`, `&`, `|`, `<`, `>`, a
   newline). Plain arguments still run through cmd.exe as before.

Nothing here is Windows-only at import time; the Windows behaviour is
selected by the `windows` parameter (default: the running platform) so
it can be unit-tested anywhere.
"""

from __future__ import annotations

import os
import re
import shutil

from .proctree import is_windows

#: Characters cmd.exe interprets when a .cmd/.bat is launched with them in
#: the command line. An argument containing any of them is refused.
UNSAFE_CMD_CHARS = '"%!^&|<>\n\r'

_SHIM_SUFFIXES = (".cmd", ".bat")
_SHIM_MAX_BYTES = 64 * 1024
# npm's cmd-shim writes:  "%_prog%"  "%dp0%\node_modules\@scope\pkg\cli.js" %*
_SHIM_SCRIPT_RE = re.compile(r'%dp0%[\\/]?([^"%\r\n]+?\.(?:c|m)?js)"',
                             re.IGNORECASE)


class LaunchError(Exception):
    """The command cannot be launched safely; the message says why."""


def is_cmd_shim(path: str) -> bool:
    return path.lower().endswith(_SHIM_SUFFIXES)


def unsafe_cmd_args(args: list[str]) -> list[tuple[int, str]]:
    """(argument index, offending character) for every argument cmd.exe
    would re-interpret. Empty when the arguments are safe."""
    bad: list[tuple[int, str]] = []
    for i, a in enumerate(args):
        for ch in a:
            if ch in UNSAFE_CMD_CHARS:
                bad.append((i, ch))
                break
    return bad


def resolve_npm_shim(shim_path: str, which=shutil.which) -> list[str] | None:
    """Turn an npm .cmd shim into the direct `[node, script.js]` command it
    runs. Returns None when the file is not a recognisable npm shim or the
    script / node binary cannot be found (the caller then falls back to
    launching the shim itself)."""
    try:
        with open(shim_path, "rb") as f:
            text = f.read(_SHIM_MAX_BYTES).decode("utf-8", "replace")
    except OSError:
        return None
    m = _SHIM_SCRIPT_RE.search(text)
    if not m:
        return None
    shim_dir = os.path.dirname(os.path.abspath(shim_path))
    rel = m.group(1).replace("\\", os.sep).replace("/", os.sep)
    script = os.path.normpath(os.path.join(shim_dir, rel))
    if not os.path.isfile(script):
        return None
    # The shim prefers a node.exe sitting next to it, then node on PATH.
    local_node = os.path.join(shim_dir, "node.exe")
    node = local_node if os.path.isfile(local_node) else which("node")
    if not node:
        return None
    return [node, script]


def resolve_command(argv: list[str], windows: bool | None = None,
                    which=shutil.which) -> list[str]:
    """Return the argv to hand to subprocess.Popen.

    argv[0] is resolved through `which` (PATHEXT-aware on Windows). On
    Windows a .cmd/.bat target is replaced by the node script it wraps
    when it is an npm shim; otherwise its arguments are checked and a
    LaunchError is raised for anything cmd.exe would mangle.
    """
    if not argv:
        return list(argv)
    windows = is_windows() if windows is None else windows
    cmd = list(argv)
    resolved = which(cmd[0])
    if not resolved and os.path.isfile(cmd[0]):
        resolved = cmd[0]  # an explicit path that which() won't vouch for
    if resolved:
        cmd[0] = resolved
    if not windows or not is_cmd_shim(cmd[0]):
        return cmd
    direct = resolve_npm_shim(cmd[0], which=which)
    if direct:
        return direct + cmd[1:]
    bad = unsafe_cmd_args(cmd[1:])
    if bad:
        idx, ch = bad[0]
        shown = {"\n": "\\n", "\r": "\\r"}.get(ch, ch)
        raise LaunchError(
            f"refusing to run {os.path.basename(cmd[0])} through cmd.exe: "
            f"argument {idx + 1} contains {shown!r}, which cmd.exe would "
            f"expand or interpret (BatBadBut-class argument injection). "
            f"Run the program's .exe or .js entry point directly, or remove "
            f"the character(s) {UNSAFE_CMD_CHARS.strip()!r} from the argument.")
    return cmd
