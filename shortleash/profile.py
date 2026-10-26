"""Per-repo budget profile: a `.shortleash.toml` next to your code.

    # .shortleash.toml
    budget = 2.50        # USD
    max_spawns = 10
    max_tokens = 800000
    timeout = 900        # seconds
    forecast = true      # early-warning lines (on by default)
    profile = "docs"     # baseline name for --anomaly

    [hook]               # only for `shortleash hook` (Claude Code hooks)
    max_spawns = 12
    loop_warn = 4        # repeat-call guard: warn / deny at N identical
    loop_deny = 8        # calls among the last loop_window (20) calls;
    loop_window = 20     # any of them turns the guard on
    velocity_usd_per_min = 1.5   # optional spend-velocity warning

`shortleash run` looks for the file in the current folder and then in each
folder above it; the first one found is used, and a flag on the command
line always wins over the file. `--no-config` ignores it, `--config
PATH` names one. The file only sets limits; it cannot run anything.

TOML is read with tomllib (Python 3.11+). Python 3.10 has none, so a small
reader that understands exactly what this file uses (numbers, strings,
true/false, one level of [table], # comments) takes its place; anything else
in the file is an error on 3.10 and ignored nowhere.
"""

from __future__ import annotations

import math
import os
import re

try:                     # Python 3.11+
    import tomllib
except ImportError:      # Python 3.10
    tomllib = None

FILE_NAME = ".shortleash.toml"
#: A file bigger than this is not a profile.
MAX_BYTES = 64 * 1024

#: key -> kind. "number" = a finite number >= 0, "int" = whole number >= 0,
#: "positive" = a finite number > 0.
TOP_KEYS = {"budget": "number", "max_spawns": "int", "max_tokens": "int",
            "timeout": "positive", "anomaly": "positive",
            "forecast": "bool", "profile": "string"}
HOOK_KEYS = {"max_spawns": "int", "loop_warn": "int", "loop_deny": "int",
             "loop_window": "int", "velocity_usd_per_min": "positive"}


class ProfileError(ValueError):
    """The profile file cannot be used; the message says what to fix."""


# -- reading -----------------------------------------------------------

_KEY = re.compile(r"[A-Za-z0-9_-]+")
_NUMBER = re.compile(r"[+-]?(?:\d[\d_]*)(?:\.\d[\d_]*)?(?:[eE][+-]?\d+)?\Z")


def _strip_comment(line: str) -> str:
    """Drop a # comment that is not inside a "..." string."""
    out, quoted, escaped = [], False, False
    for ch in line:
        if quoted:
            out.append(ch)
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                quoted = False
            continue
        if ch == "#":
            break
        if ch == '"':
            quoted = True
        out.append(ch)
    return "".join(out).strip()


def _value(text: str, where: str):
    if text in ("true", "false"):
        return text == "true"
    if len(text) >= 2 and text[0] == text[-1] == '"':
        body = text[1:-1]
        if "\\" in body or '"' in body:
            raise ProfileError(f"{where}: a backslash or quote inside a "
                               f"\"string\" is not understood here")
        return body
    if len(text) >= 2 and text[0] == text[-1] == "'":
        return text[1:-1]
    if re.fullmatch(r"[+-]?(inf|nan)", text):     # TOML has them; validate() refuses
        return float(text)
    if _NUMBER.match(text):
        clean = text.replace("_", "")
        if re.fullmatch(r"[+-]?\d+", clean):
            return int(clean)
        return float(clean)
    raise ProfileError(f"{where}: cannot read the value {text!r} (numbers, "
                       f"\"strings\", true and false are understood)")


def parse_simple(text: str) -> dict:
    """The tiny TOML reader used on Python 3.10 (also testable everywhere)."""
    data: dict = {}
    table = data
    for number, raw in enumerate(text.split("\n"), 1):
        line = _strip_comment(raw.rstrip("\r"))
        where = f"line {number}"
        if not line:
            continue
        if line.startswith("["):
            m = re.fullmatch(r"\[\s*([A-Za-z0-9_-]+)\s*\]", line)
            if not m:
                raise ProfileError(f"{where}: only a plain [table] is "
                                   f"understood, got {line!r}")
            name = m.group(1)
            if name in data:
                raise ProfileError(f"{where}: [{name}] appears twice")
            table = data[name] = {}
            continue
        key, eq, value = line.partition("=")
        key, value = key.strip(), value.strip()
        if not eq or not _KEY.fullmatch(key) or not value:
            raise ProfileError(f"{where}: expected  key = value, got {line!r}")
        if key in table:
            raise ProfileError(f"{where}: {key!r} is set twice")
        table[key] = _value(value, where)
    return data


def parse(text: str) -> dict:
    """The file's content as a dict (tomllib if there is one)."""
    if tomllib is not None:
        try:
            return tomllib.loads(text)
        except tomllib.TOMLDecodeError as e:
            raise ProfileError(f"not valid TOML ({e})") from None
    return parse_simple(text)


# -- checking ------------------------------------------------------------

def _check(key: str, value, kind: str, where: str):
    name = f"{where}{key}"
    if kind == "bool":
        if not isinstance(value, bool):
            raise ProfileError(f"{name} must be true or false (got {value!r})")
        return value
    if kind == "string":
        if not isinstance(value, str) or not value.strip() or len(value) > 80:
            raise ProfileError(f"{name} must be a short text (got {value!r})")
        return value
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ProfileError(f"{name} must be a number (got {value!r})")
    if not math.isfinite(value):
        raise ProfileError(f"{name} must be a finite number (got {value!r})")
    if kind == "int":
        if value != int(value) or value < 0:
            raise ProfileError(f"{name} must be a whole number >= 0 "
                               f"(got {value!r})")
        return int(value)
    if kind == "positive":
        if value <= 0:
            raise ProfileError(f"{name} must be greater than 0 (got {value!r})")
        return float(value)
    if value < 0:
        raise ProfileError(f"{name} must be >= 0 (got {value!r})")
    return float(value)


def validate(data: dict) -> dict:
    """The profile as {"budget": ..., "hook": {"max_spawns": ...}}, or
    ProfileError naming the key. Unknown keys are an error: a typo
    (`max_spawn = 5`) must not silently leave a run unguarded."""
    if not isinstance(data, dict):
        raise ProfileError("the top level must be a table of settings")
    out: dict = {}
    for key, value in data.items():
        if key == "hook":
            if not isinstance(value, dict):
                raise ProfileError("[hook] must be a table")
            hook = {}
            for k, v in value.items():
                if k not in HOOK_KEYS:
                    raise ProfileError(
                        f"[hook] has no setting {k!r} (known: "
                        f"{', '.join(sorted(HOOK_KEYS))})")
                hook[k] = _check(k, v, HOOK_KEYS[k], "hook.")
            out["hook"] = hook
        elif key in TOP_KEYS:
            out[key] = _check(key, value, TOP_KEYS[key], "")
        else:
            raise ProfileError(
                f"unknown setting {key!r} (known: "
                f"{', '.join(sorted([*TOP_KEYS, 'hook']))})")
    return out


# -- finding -------------------------------------------------------------

def find(start: str | None = None) -> str | None:
    """The nearest .shortleash.toml in ``start`` (default: the current
    folder) or above it; None when there is none."""
    here = os.path.abspath(start or os.getcwd())
    while True:
        path = os.path.join(here, FILE_NAME)
        if os.path.isfile(path):
            return path
        parent = os.path.dirname(here)
        if parent == here:
            return None
        here = parent


def load(path: str) -> dict:
    """Read and check one profile file. ProfileError (with the path) on any
    problem, OSError when it cannot be read."""
    with open(path, "rb") as f:
        raw = f.read(MAX_BYTES + 1)
    if len(raw) > MAX_BYTES:
        raise ProfileError(f"{path}: larger than {MAX_BYTES // 1024} KB, "
                           f"not a profile")
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise ProfileError(f"{path}: not UTF-8 text (save it as UTF-8)") \
            from None
    try:
        return validate(parse(text))
    except ProfileError as e:
        raise ProfileError(f"{path}: {e}") from None
