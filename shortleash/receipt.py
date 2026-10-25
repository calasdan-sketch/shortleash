"""Per-run receipt: what happened, what it cost, why it stopped."""

from __future__ import annotations

import json
import math
import os
import re
import stat
from datetime import datetime, timezone

from . import __version__

# Any argument whose NAME matches one of these (case-insensitive) gets its
# VALUE replaced with *** in receipts. Matches --api-key, api_key=, -token,
# --db-password value, etc.
# "auth" counts only as a whole word part (--auth, auth_header, x-auth-key,
# oauth, authorization, authentication): "--author" is not a secret.
_SECRET_NAME_RE = re.compile(
    r"key|token|secret|password|passwd|bearer|credential|cookie"
    r"|(?<![A-Za-z])o?auth(?:orization|entication)?(?![A-Za-z])",
    re.IGNORECASE)
#: Flags whose value is credentials although the name does not say so
#: (curl -u user:pass, --proxy-user).
_SECRET_VALUE_FLAGS = {"-u", "--user", "--proxy-user", "-U"}
#: Flags whose value is an HTTP header line ("Authorization: Bearer ...").
_HEADER_FLAGS = {"-H", "--header", "--proxy-header"}
#: Secret-looking tokens inside ANY argument, wherever they appear:
#: sk-... API keys, Bearer/Basic credentials, GitHub / Slack / AWS tokens.
_SECRET_VALUE_RE = re.compile(
    r"(?<![A-Za-z0-9])sk-[A-Za-z0-9_\-]{8,}"
    r"|(?<![A-Za-z0-9])(?i:bearer)\s+[A-Za-z0-9._~+/=\-]{8,}"
    r"|(?<![A-Za-z0-9])(?i:basic)\s+(?=[A-Za-z0-9+/]*[A-Z0-9+/=])"
    r"[A-Za-z0-9._~+/=\-]{8,}"
    r"|gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}"
    r"|xox[abprs]-[A-Za-z0-9-]{10,}|AKIA[0-9A-Z]{16}")
#: user:password@ inside a URL (https://user:pass@host/x).
_URL_USERINFO_RE = re.compile(r"(?<=://)[^/\s:@]*:[^/\s@]*@")
#: A short flag with its value glued on: curl -uuser:pass.
_GLUED_USER_RE = re.compile(r"^(-[uU])(?!-)(.+:.*)$")
_COLON_RE = re.compile(r"^([A-Za-z0-9_][A-Za-z0-9_-]*):(?!//)(.+)$", re.DOTALL)
_FLAG_EQ_RE = re.compile(r"^(--?)([A-Za-z0-9_][A-Za-z0-9_-]*)=(.*)$")
_BARE_EQ_RE = re.compile(r"^([A-Za-z0-9_][A-Za-z0-9_-]*)=(.*)$")
_FLAG_RE = re.compile(r"^(--?)([A-Za-z0-9_][A-Za-z0-9_-]*)$")

#: Flags whose next argument is the prompt text (Claude Code: `-p`/`--print`
#: is a flag and the prompt follows it in the usual `claude -p "..."`).
PROMPT_FLAGS = ("-p", "--print", "--prompt")
REDACTED_PROMPT = "<redacted>"


def _is_secret_name(name: str) -> bool:
    return _SECRET_NAME_RE.search(name) is not None


def _redact_header(text: str) -> str:
    """`Authorization: Bearer x` -> `Authorization: ***` (name kept)."""
    name, sep, _rest = text.partition(":")
    if sep and (_is_secret_name(name) or name.strip().lower()
                in ("proxy-authorization", "x-api-key")):
        return f"{name}: ***"
    return text


def _scrub(arg: str) -> str:
    return _URL_USERINFO_RE.sub("***@", _SECRET_VALUE_RE.sub("***", arg))


def redact_argv(argv: list[str], redact_prompt: bool = False) -> list[str]:
    return [_scrub(a) for a in _redact_argv(argv, redact_prompt)]


def _redact_argv(argv: list[str], redact_prompt: bool = False) -> list[str]:
    """Return argv with secret values replaced by ***.

    Never mutate the original. Handles three shapes:
      --api-key sk-123   -> ["--api-key", "***"]   (flag + value pair)
      --api-key=sk-123   -> ["--api-key=***"]
      api_key=sk-123     -> ["api_key=***"]
    Matching is substring on the argument name (e.g. "monkey" would also
    match — over-redaction is the safe direction).

    The PROMPT is not a secret by this rule and is stored verbatim: with
    ``redact_prompt`` the argument following -p/--print/--prompt (and a
    --prompt=... value) becomes "<redacted>" instead. A prompt passed any
    other way (bare positional, stdin, a file) is not recognised.
    """
    out: list[str] = []
    redact_next = False
    header_next = False
    prompt_next = False
    for a in argv:
        if redact_next:
            out.append("***")
            redact_next = False
            continue
        if header_next:
            out.append(_redact_header(a))
            header_next = False
            continue
        if a in _HEADER_FLAGS:
            out.append(a)
            header_next = True
            continue
        if a in _SECRET_VALUE_FLAGS:
            out.append(a)
            redact_next = True
            continue
        if prompt_next:
            prompt_next = False
            if not a.startswith("-"):
                out.append(REDACTED_PROMPT)
                continue
        if redact_prompt:
            if a in PROMPT_FLAGS:
                out.append(a)
                prompt_next = True
                continue
            eq_flag = next((f for f in PROMPT_FLAGS if a.startswith(f + "=")),
                           None)
            if eq_flag:
                out.append(f"{eq_flag}={REDACTED_PROMPT}")
                continue
        m = _GLUED_USER_RE.match(a)
        if m:
            out.append(m.group(1) + "***")
            continue
        m = _FLAG_EQ_RE.match(a)
        if m:
            prefix, name, value = m.groups()
            if prefix + name in _HEADER_FLAGS:
                value = _redact_header(value)
            elif prefix + name in _SECRET_VALUE_FLAGS:
                value = "***"
            out.append(f"{prefix}{name}="
                       + ("***" if _is_secret_name(name) else value))
            continue
        m = _BARE_EQ_RE.match(a)
        if m:
            name, value = m.groups()
            out.append(f"{name}="
                       + ("***" if _is_secret_name(name) else value))
            continue
        m = _COLON_RE.match(a)
        if m and _is_secret_name(m.group(1)):
            out.append(f"{m.group(1)}:***")
            continue
        m = _FLAG_RE.match(a)
        if m and _is_secret_name(m.group(2)):
            out.append(a)
            redact_next = True
            continue
        out.append(a)
    return out


def _sanitize(obj):
    """A copy of ``obj`` with every non-finite float replaced by None: JSON
    has no Infinity / NaN, and a bare `Infinity` token breaks every strict
    parser that reads the receipt."""
    if isinstance(obj, float):
        return obj if math.isfinite(obj) else None
    if isinstance(obj, dict):
        return {k: _sanitize(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_sanitize(v) for v in obj]
    return obj


def build_receipt(
    *,
    command: list[str],
    started_at: float,
    ended_at: float,
    breached: bool,
    breach_reason: str | None,
    cost_usd: float | None,
    cost_estimated: bool,
    usage: dict,
    num_turns: int,
    spawns: int,
    exit_code: int | None,
    model: str,
    session_id: str,
    agent: str = "",
    profile: str = "",
    baseline: dict | None = None,
    telemetry_seen: bool = True,
    usage_events: int = 0,
    enforced: list[str] | None = None,
    not_enforced: list[str] | None = None,
    pricing: dict | None = None,
    warnings: list[str] | None = None,
    redact_prompt: bool = False,
    agent_error: bool = False,
    agent_error_text: str = "",
    hint: str | None = None,
    child_returncode: int | None = None,
    anomaly_armed: dict | None = None,
    enforced_live: dict | None = None,
    breach_live: bool | None = None,
    price_assumed: bool = False,
) -> dict:
    warnings = list(warnings or [])
    if cost_usd is not None and not math.isfinite(cost_usd):
        cost_usd = None
        warnings.append("the cost was not a finite number (absurd usage was "
                        "reported) and is shown as n/a")
    return {
        "tool": "shortleash",
        "version": __version__,
        # Never persist raw argv: redact anything that looks like a secret.
        # The prompt itself is kept unless --redact-prompt was given.
        "command": redact_argv(command, redact_prompt=redact_prompt),
        "prompt_redacted": bool(redact_prompt),
        "agent": agent,
        "profile": profile,
        "baseline": baseline,
        "started_at": datetime.fromtimestamp(started_at, tz=timezone.utc).isoformat(),
        "ended_at": datetime.fromtimestamp(ended_at, tz=timezone.utc).isoformat(),
        "duration_s": round(ended_at - started_at, 2),
        "breached": breached,
        "breach_reason": breach_reason,
        # None when the child produced no usage telemetry: there is no
        # cost figure, estimated or otherwise — $0.00 would be a lie.
        "cost_usd": None if cost_usd is None else round(cost_usd, 6),
        "cost_estimated": cost_estimated,
        "tokens": {
            "input": usage.get("input", 0),
            "output": usage.get("output", 0),
            "cache_write": usage.get("cache_write", 0),
            "cache_read": usage.get("cache_read", 0),
        },
        "num_turns": num_turns,
        "subagent_spawns": spawns,
        "exit_code": exit_code,
        # What the child itself returned, untouched: a negative "killed by
        # signal N" on POSIX, a 32-bit status on Windows; None when the
        # tree would not die. exit_code above is what the leash exits with.
        "child_returncode": child_returncode,
        "model": model,
        "session_id": session_id,
        # Trust boundary, made explicit: which caps actually held.
        "telemetry_seen": telemetry_seen,
        "usage_events": usage_events,
        "enforced": list(enforced or []),
        "not_enforced": list(not_enforced or []),
        # For each cap in `enforced`: could it stop the run while it ran
        # (true), or was it only checked against the agent's final report
        # once the run was over (false)? Only the wall-clock limit and caps
        # fed by per-message usage events are live.
        "enforced_live": dict(enforced_live or {}),
        # None: not breached. True: the leash ended the run. False: the
        # breach was only found after the run was over (nothing stopped).
        "breach_live": breach_live,
        # --anomaly: {"armed", "runs", "min_runs", "multiplier"}, or None
        # when anomaly mode was not requested. Un-armed means no anomaly
        # cap held (it is then in neither enforced nor not_enforced).
        "anomaly": anomaly_armed,
        "pricing": pricing,
        # True when usage was priced but no table entry names the model: the
        # estimate behind the caps used the fallback rate (a warning says so).
        "price_assumed": bool(price_assumed),
        "warnings": warnings,
        # The agent's own verdict: a stream-json `result` with is_error
        # true is a failed run even when the process exited 0.
        "agent_error": bool(agent_error),
        "agent_error_text": agent_error_text or "",
        # One line of advice printed under STOPPED (e.g. how to leave the
        # anomaly ratchet), or None.
        "hint": hint,
    }


NO_TELEMETRY_MSG = "no usage data seen"


def no_telemetry_line(receipt: dict) -> str:
    """The honest one-liner for a run whose child sent no usage events."""
    enforced = receipt.get("enforced") or []
    missing = receipt.get("not_enforced") or []
    if "timeout" in enforced:
        line = (f"{NO_TELEMETRY_MSG} — only the wall-clock/time limit "
                f"was enforced")
    else:
        line = (f"{NO_TELEMETRY_MSG} — no wall-clock/time limit was set "
                f"either, so nothing was enforced")
    if missing:
        line += f"; NOT enforced: {', '.join(missing)}"
    return line


def format_human(receipt: dict) -> str:
    t = receipt["tokens"]
    total_tokens = t["input"] + t["output"] + t["cache_write"] + t["cache_read"]
    telemetry = receipt.get("telemetry_seen", True)
    if telemetry and receipt["cost_usd"] is None:
        cost_line = "  cost:      n/a (not a finite number)"
    elif receipt["cost_usd"] is None or not telemetry:
        cost_line = f"  cost:      n/a ({NO_TELEMETRY_MSG})"
    else:
        cost_line = (f"  cost:      ${receipt['cost_usd']:.4f}"
                     + (" (estimated)" if receipt["cost_estimated"]
                        else " (reported)"))
    lines = [
        "",
        "── shortleash receipt ──────────────────────",
        cost_line,
        f"  tokens:    {total_tokens:,} "
        f"(in={t['input']:,} out={t['output']:,} "
        f"cache_r={t['cache_read']:,} cache_w={t['cache_write']:,})",
        f"  turns:     {receipt['num_turns']}   spawns: {receipt['subagent_spawns']}",
        f"  duration:  {receipt['duration_s']}s   exit: {receipt['exit_code']}",
    ]
    pricing = receipt.get("pricing")
    if pricing and telemetry and receipt.get("cost_estimated", True):
        lines.append(f"  pricing:   {pricing.get('source', 'table')} as of "
                     f"{pricing.get('as_of', '?')} — verify against "
                     f"provider pricing")
    if receipt["breached"]:
        # EXCEEDED, not STOPPED, when the breach was only found after the
        # run was over: nothing was stopped, and saying so would be a lie.
        verb = "EXCEEDED:" if receipt.get("breach_live") is False else "STOPPED: "
        lines.append(f"  {verb}  {receipt['breach_reason']}")
        if receipt.get("hint"):
            lines.append(f"  hint:      {receipt['hint']}")
    elif receipt.get("agent_error"):
        detail = receipt.get("agent_error_text") or "no detail"
        lines.append(f"  FAILED:    the agent reported is_error "
                     f"({detail})")
    elif not telemetry:
        # Never print the green tick here: a cost cap cannot have held
        # when the child never reported any usage.
        lines.append(f"  WARNING:   {no_telemetry_line(receipt)}")
    elif receipt.get("not_enforced"):
        # Some of the caps asked for were checked against nothing.
        lines.append(f"  WARNING:   NOT enforced: "
                     f"{', '.join(receipt['not_enforced'])} (nothing to "
                     f"check against)")
    else:
        lines.append("  finished within guardrails ✓")
    for w in receipt.get("warnings") or []:
        lines.append(f"  warning:   {w}")
    baseline = receipt.get("baseline")
    anomaly = receipt.get("anomaly")
    if baseline:
        lines.append(f"  baseline:  typical ${baseline['median_cost']:.4f} / "
                     f"{baseline['median_tokens']:,} tokens "
                     f"({baseline['runs']} past runs)")
    elif anomaly and not anomaly.get("armed"):
        lines.append(f"  anomaly:   not armed ({anomaly.get('runs', 0)} of "
                     f"{anomaly.get('min_runs', '?')} runs) — nothing was "
                     f"enforced by it")
    lines.append("──────────────────────────────────────────────────")
    return "\n".join(lines)


def prepare_receipt_path(path: str) -> None:
    """Make sure a receipt can be written to ``path`` before the run
    starts: create the directory and test-open the file (a pre-existing
    file is left as it is; one we created for the test is removed again).
    Raises OSError with the reason when it cannot be done."""
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, exist_ok=True)
    existed = os.path.exists(path)
    with open(path, "a", encoding="utf-8"):
        pass
    if not existed:
        os.remove(path)


def write_receipt(receipt: dict, path: str) -> None:
    # The command line (even redacted) and the session are private: 0600.
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        if stat.S_ISREG(os.fstat(fd).st_mode):
            os.chmod(path, 0o600)   # a file that already existed too
    except OSError:
        pass
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(_sanitize(receipt), f, indent=2, allow_nan=False)
