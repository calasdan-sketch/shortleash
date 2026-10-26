"""Stop Runaway Agents — a circuit breaker for AI coding-agent runs.

Wraps an agent command, tails its stream-json telemetry in real time,
and kills the run the moment it breaches a guardrail — budget, spawn
cap, token cap, or wall-clock timeout. Emits a per-run receipt.

Subcommands:
  run    the leash (this tool's purpose)
  hook   Claude Code hooks: a spawn limit inside an ordinary session
         (`hook install`, `hook uninstall`, `hook status`),
         with an optional repeat-call guard (`hook install --loop-guard`)
  fleet  see who holds a fleet claim (`fleet list`)
  cache  a separate helper: the prompt-caching optimizer (cacheopt.py).
         It shares nothing with the leash but the package; see the README
         section of the same name.
"""

import argparse
import errno
import atexit
import json
import math
import os
import sys
import time

from . import cacheopt
from . import fleet as fleet_mod
from . import guard as guard_mod
from . import hookmode
from . import loopguard as loopguard_mod
from . import pricing
from . import profile as profile_mod
from . import __version__

DEFAULT_ANOMALY = 3.0


def _harden_streams() -> None:
    """Never crash on a console that cannot encode the receipt or the
    agent's output (Windows cp1252 consoles, piped output under odd
    locales). Python's stderr already replaces unencodable characters;
    make stdout do the same instead of raising UnicodeEncodeError."""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        # Both handlers raise UnicodeEncodeError on a character the code
        # page lacks: "strict" (the default) and "surrogateescape" (what a
        # piped stdout reports on Windows 11 / Python 3.11, with cp1252).
        if reconfigure is None or getattr(stream, "errors", None) not in (
                "strict", "surrogateescape"):
            continue
        try:
            reconfigure(errors="replace")
        except (OSError, ValueError):
            pass


def _add_run_parser(sub) -> None:
    r = sub.add_parser("run", help="Run a command under guardrails.")
    r.add_argument("--budget", type=float, default=None,
                   help="Max estimated spend in USD before the run is killed "
                        "(a finite number >= 0).")
    r.add_argument("--max-spawns", type=int, default=None,
                   help="Spawn-storm guard: stop the run when the agent "
                        "starts more than this many subagents. It stops "
                        "quickly, not instantly: subagents already started "
                        "may have spent tokens (a whole number >= 0).")
    r.add_argument("--max-tokens", type=int, default=None,
                   help="Max total tokens (in+out+cache) before the run is killed.")
    r.add_argument("--timeout", type=float, default=None,
                   help="Max wall-clock seconds before the run is killed "
                        "(a finite number > 0).")
    r.add_argument("--grace-period", type=float, default=5.0,
                   help="Seconds between SIGTERM and SIGKILL on breach "
                        "(POSIX; Windows force-kills the tree at once), "
                        "and how long to wait for stray child processes "
                        "after the command exits (a finite number >= 0).")
    r.add_argument("--pricing", type=str, default=None,
                   help="JSON file overriding the fallback pricing table "
                        f"(or set ${pricing.PRICING_ENV}). The built-in "
                        f"table is as of {pricing.PRICING_AS_OF} — verify "
                        "against provider pricing.")
    r.add_argument("--receipt", type=str, default=None,
                   help="Write the per-run receipt JSON to this path.")
    r.add_argument("--redact-prompt", action="store_true",
                   help="Replace the prompt (the argument after -p/--print/"
                        "--prompt) with <redacted> in the receipt. Prompts "
                        "are stored verbatim otherwise.")
    r.add_argument("--quiet", action="store_true",
                   help="Leave the startup banner, the human-readable "
                        "receipt and the one-line notices off stderr. The "
                        "agent's output, the --receipt JSON, errors and "
                        "fleet warnings are not affected.")
    r.add_argument("--anomaly", type=float, default=None, nargs="?",
                   const=DEFAULT_ANOMALY, metavar="MULT",
                   help="Opt in to anomaly mode: trip when live cost/tokens "
                        f"exceed MULT x the historical median for this agent "
                        f"(MULT defaults to {DEFAULT_ANOMALY}). Arms only "
                        "after --min-history past runs. Off unless given.")
    r.add_argument("--no-anomaly", action="store_true",
                   help="Accepted for compatibility: anomaly mode is already "
                        "off unless --anomaly is given.")
    r.add_argument("--profile", type=str, default="",
                   help="Baseline grouping key (default: the agent binary's "
                        "name in lower case, without .exe/.cmd/.bat/.com/"
                        ".ps1).")
    r.add_argument("--history-path", type=str, default=None, metavar="FILE",
                   help="File that finished runs are recorded in and that "
                        "--anomaly reads its baseline from (default: "
                        "history.jsonl in ~/.shortleash, or in "
                        "$SHORTLEASH_HOME).")
    r.add_argument("--min-history", type=int, default=5,
                   help="Past runs needed before anomaly mode arms (at least "
                        "1: the baseline is the median of them; default 5).")
    r.add_argument("--fleet", action="store_true",
                   help="Hold a fleet claim on paths for the run (merged "
                        "from claimlock): warns on conflict, releases on "
                        "exit. Default claim: the current directory.")
    r.add_argument("--claim", nargs="+", default=None, metavar="PATH",
                   help="Paths to claim with --fleet (default: cwd).")
    r.add_argument("--fleet-owner", default="",
                   help="Owner label on the fleet claim.")
    r.add_argument("--fleet-ttl", type=int, default=fleet_mod.DEFAULT_TTL,
                   help=f"Claim TTL in seconds, 1 to {fleet_mod.MAX_TTL} "
                        f"(default {fleet_mod.DEFAULT_TTL}); a value outside "
                        "that range is refused.")
    r.add_argument("--no-forecast", action="store_true",
                   help="Turn the early-warning lines off (a budget or token "
                        "cap, 20%%/50%%/80%% used, at the pace so far).")
    r.add_argument("--config", metavar="FILE", default=None,
                   help="Read limits from this .shortleash.toml instead of "
                        "looking for one in the current folder and above.")
    r.add_argument("--no-config", action="store_true",
                   help="Ignore any .shortleash.toml.")
    r.add_argument("command", nargs=argparse.REMAINDER,
                   help="Command to run, after -- : shortleash run --budget 2 -- claude -p ...")


def _add_fleet_parser(sub) -> None:
    f = sub.add_parser("fleet", help="Fleet claims held on this machine.")
    fsub = f.add_subparsers(dest="fleet_cmd", required=True)
    fsub.add_parser("list", help="Show who currently holds which path.")


def _add_hook_parser(sub) -> None:
    h = sub.add_parser(
        "hook", help="Claude Code hooks: a spawn limit for ordinary "
                     "sessions.")
    hsub = h.add_subparsers(dest="hook_cmd", required=True)
    ins = hsub.add_parser(
        "install", help="Add the two hooks to your Claude Code settings "
                        "(nothing of yours is changed or removed).")
    ins.add_argument("--max-spawns", type=int, default=None, metavar="N",
                     help="Deny the (N+1)th subagent start of a session. "
                          "Without it the limit comes from $SHORTLEASH_MAX_"
                          "SPAWNS or a .shortleash.toml.")
    ins.add_argument("--loop-guard", action="store_true",
                     help="Also watch for a repeated tool call: warn at the "
                          "4th identical call among the last 20 and refuse "
                          "at the 8th. Makes the PreToolUse hook run for "
                          "every tool.")
    ins.add_argument("--no-loop-guard", action="store_true",
                     help="Switch the repeat-call guard off again.")
    ins.add_argument("--loop-warn", type=int, default=None, metavar="N",
                     help="Warn at N identical calls (default 4; 0 = never). "
                          "Implies --loop-guard.")
    ins.add_argument("--loop-deny", type=int, default=None, metavar="M",
                     help="Refuse at M identical calls (default 8; 0 = "
                          "never). Implies --loop-guard.")
    ins.add_argument("--loop-window", type=int, default=None, metavar="W",
                     help="Count among the last W tool calls (default 20). "
                          "Implies --loop-guard.")
    ins.add_argument("--velocity-usd-per-min", type=float, default=None,
                     metavar="USD", help="Warn when the session's estimated "
                     "spend over the last five minutes exceeds USD per "
                     "minute. Implies --loop-guard.")
    ins.add_argument("--scope", choices=["user", "project", "local"],
                     default="user",
                     help="user: ~/.claude/settings.json (default); project: "
                          "./.claude/settings.json; local: "
                          "./.claude/settings.local.json.")
    ins.add_argument("--settings", metavar="FILE", default=None,
                     help="Settings file to change (overrides --scope).")
    ins.add_argument("--dry-run", action="store_true",
                     help="Print the hooks and change nothing.")
    un = hsub.add_parser("uninstall", help="Remove exactly the hooks "
                                           "`hook install` added.")
    un.add_argument("--scope", choices=["user", "project", "local"],
                    default="user")
    un.add_argument("--settings", metavar="FILE", default=None)
    hsub.add_parser("status", help="Show what recent sessions have started.")
    # Called by Claude Code, not by people:
    hsub.add_parser("pretool", help=argparse.SUPPRESS)
    hsub.add_parser("subagent-start", help=argparse.SUPPRESS)


def _add_cache_parser(sub) -> None:
    # A separate helper that happens to live in this package: it never
    # touches the leash, the receipt or the history.
    c = sub.add_parser("cache", help="Prompt-caching optimizer (separate "
                                     "helper, unrelated to the leash).")
    csub = c.add_subparsers(dest="cache_cmd", required=True)
    csub.add_parser("guide", help="Provider caching docs + the core rule.")
    ck = csub.add_parser("check", help="Report how cacheable a prompt is.")
    ck.add_argument("-i", "--in", dest="in_file", required=True,
                    help="Prompt file with {{variables}}.")
    cm = csub.add_parser("make", help="Restructure a prompt for caching.")
    cm.add_argument("-i", "--in", dest="in_file", required=True,
                    help="Prompt file with {{variables}}.")
    cm.add_argument("-o", "--out", dest="out_file", default=None,
                    help="Write optimized prompt here (default: stdout).")
    cm.add_argument("--provider", default="all",
                    choices=["all", "anthropic", "openai", "gemini", "deepseek"],
                    help="Emit integration snippet for provider(s).")


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="shortleash",
        description=(
            "Circuit breaker for AI coding-agent runs. Wraps a command, "
            "enforces guardrails live, kills on breach."
        ),
    )
    p.add_argument("--version", action="version",
                   version=f"%(prog)s {__version__}")
    sub = p.add_subparsers(dest="cmd", required=True)
    _add_run_parser(sub)
    _add_hook_parser(sub)
    ini = sub.add_parser(
        "init", help="One step: install the hook and write a starter "
                     ".shortleash.toml ($5, 10 spawns, 30 minutes).")
    ini.add_argument("--scope", choices=["user", "project", "local"],
                     default="user")
    ini.add_argument("--settings", metavar="FILE", default=None)
    ini.add_argument("--dry-run", action="store_true",
                     help="Say what would be done; change nothing.")
    rep = sub.add_parser(
        "report", help="Cost of guarded runs over the last N days, from "
                       "the history file.")
    rep.add_argument("--days", type=int, default=7, metavar="N")
    rep.add_argument("--history-path", default=None, metavar="FILE")
    rep.add_argument("--json", action="store_true")
    _add_fleet_parser(sub)
    _add_cache_parser(sub)
    return p


def _has_flag(argv: list[str], flag: str) -> bool:
    """Is `flag` given, as `--flag VALUE` or as `--flag=VALUE`?"""
    return any(a == flag or a.startswith(flag + "=") for a in argv)


def _maybe_inject_stream_json(argv: list[str]) -> list[str]:
    """If wrapping `claude` without stream-json flags, add them.

    The guard needs the telemetry stream to enforce caps. This keeps the
    common case to: shortleash run --budget 2 -- claude -p "do thing"
    A format the user chose (`--output-format json` or `--output-format=json`)
    is left alone.
    """
    if not argv:
        return argv
    prog = guard_mod.agent_name(argv[0])
    if prog == "claude" and not _has_flag(argv, "--output-format"):
        return argv + ["--output-format", "stream-json", "--verbose"]
    return argv


def _cmd_cache(args) -> int:
    if args.cache_cmd == "guide":
        print(cacheopt.guide_text())
        return 0
    try:
        with open(args.in_file, "rb") as f:
            data = f.read()
    except OSError as e:
        print(f"error: can't read {args.in_file}: {e}", file=sys.stderr)
        return 2
    try:
        text = cacheopt.decode_prompt(data)
    except ValueError as e:
        print(f"error: {args.in_file} {e}", file=sys.stderr)
        return 2
    if args.cache_cmd == "check":
        a = cacheopt.analyze(text)
        print(f"\n  cache check: {args.in_file}\n")
        print(f"    placeholders : {', '.join(a['placeholders']) or 'none'}")
        print(f"    static chars : {a['static_chars']}/{a['total_chars']} "
              f"({a['static_ratio']:.0%} static)")
        if a["n_placeholders"] == 0:
            print("    verdict      : fully cacheable already ✓")
        elif a["static_ratio"] >= 0.7:
            print("    verdict      : good shape — run `cache make` to "
                  "lock it in ✓")
        else:
            print("    verdict      : leaky — most of the prompt changes per "
                  "request; `cache make` can help")
        print()
        return 0
    # make
    optimized, notes = cacheopt.restructure(text)
    out = ["", "  OPTIMIZED PROMPT", "  " + "─" * 40, optimized,
           "  " + "─" * 40]
    for n in notes:
        out.append(f"  note: {n}")
    providers = (["anthropic", "openai", "gemini", "deepseek"]
                 if args.provider == "all" else [args.provider])
    for prov in providers:
        out += ["", f"  ── {prov} ──", cacheopt.provider_snippet(prov)]
    rendered = "\n".join(out)
    if args.out_file:
        try:
            with open(args.out_file, "w", encoding="utf-8") as f:
                f.write(optimized)
        except OSError as e:
            print(f"error: can't write {args.out_file}: {e}", file=sys.stderr)
            return 2
        print(rendered)
        print(f"\n  optimized prompt written to {args.out_file}")
    else:
        print(rendered)
    return 0


def _cmd_fleet(args) -> int:
    try:
        conn = fleet_mod.connect()
        try:
            claims = fleet_mod.active_claims(conn)
        finally:
            conn.close()
    except fleet_mod.FleetUnavailable as e:
        # not a broken database: this Python cannot do fleet claims at all
        print(f"shortleash: error: {e}", file=sys.stderr)
        return 2
    except fleet_mod.db_errors() as e:
        print(f"shortleash: error: the fleet database is unusable "
              f"({_fleet_db()}): {e}", file=sys.stderr)
        return 1
    if not claims:
        print("no active fleet claims")
        return 0
    for c in claims:
        line = (f"{c['claim_id']}  {c['path']}  held by "
                f"{c['owner'] or '?'}  ({c['ttl_left']}s left)")
        if c["note"]:
            line += f"  — {c['note']}"
        print(line)
    return 0


def _apply_profile(args) -> int:
    """Fill the limits the command line left unset from .shortleash.toml.
    Returns an exit code (2) when the file is unusable, else 0. The flag
    always wins over the file."""
    args.profile_file = None
    if args.no_config:
        return 0
    try:
        path = args.config or profile_mod.find()
        if not path:
            return 0
        data = profile_mod.load(path)
    except (OSError, profile_mod.ProfileError) as e:
        text = str(e) if isinstance(e, profile_mod.ProfileError) \
            else f"cannot read {args.config}: {e.strerror or e}"
        print(f"shortleash: error: {text}", file=sys.stderr)
        return 2
    args.profile_file = path
    for key in ("budget", "max_spawns", "max_tokens", "timeout"):
        if getattr(args, key) is None and key in data:
            setattr(args, key, data[key])
    if args.anomaly is None and not args.no_anomaly and "anomaly" in data:
        args.anomaly = data["anomaly"]
    if not args.profile and "profile" in data:
        args.profile = data["profile"]
    if not args.no_forecast and data.get("forecast") is False:
        args.no_forecast = True
    return 0


def _read_stdin() -> str:
    try:
        return sys.stdin.read(1024 * 1024)
    except (OSError, ValueError, AttributeError, UnicodeDecodeError):
        return ""


def _cmd_report(args) -> int:
    from . import costreport
    if not 1 <= args.days <= 3650:
        print("shortleash: error: --days must be between 1 and 3650",
              file=sys.stderr)
        return 2
    r = costreport.summarize(args.history_path, args.days)
    if args.json:
        print(json.dumps(r, indent=2))
    else:
        for line in costreport.to_text(r):
            print(line)
    return 0


def _cmd_init(args) -> int:
    """Hook + starter profile in one step. Never overwrites a profile that
    exists; the hook install is the same (idempotent) one as `hook install`."""
    from . import costreport
    target = os.path.join(os.getcwd(), profile_mod.FILE_NAME)
    have = profile_mod.find()
    cap, _ = hookmode.resolve_cap(os.getcwd())
    plan = [f"hook: {hookmode.settings_path(args.scope) if not args.settings else args.settings}",
            ("profile: " + (f"keeps {have}" if have else f"writes {target}"))]
    if args.dry_run:
        print("shortleash init (dry run), would do:")
        for line in plan:
            print("  " + line)
        return 0
    ns = argparse.Namespace(max_spawns=None if (have or cap is not None) else 10,
                            scope=args.scope, settings=args.settings,
                            dry_run=False)
    rc = _cmd_hook(argparse.Namespace(hook_cmd="install", **vars(ns)))
    if rc:
        return rc
    if have:
        print(f"Kept the profile you already have: {have}")
    else:
        try:
            with open(target, "x", encoding="utf-8", newline="\n") as f:
                f.write(costreport.DEFAULT_PROFILE)
            print(f"Wrote {target}: budget $5, 10 spawns, 30 minutes. "
                  f"`shortleash run -- <agent>` enforces all three; the hook "
                  f"enforces the spawn limit inside ordinary sessions.")
        except FileExistsError:
            print(f"Kept the profile you already have: {target}")
        except OSError as e:
            print(f"shortleash: error: cannot write {target}: "
                  f"{e.strerror or e}", file=sys.stderr)
            return 2
    return 0


def _hook_settings_file(args) -> str:
    return args.settings or hookmode.settings_path(args.scope)


def _cmd_hook(args) -> int:
    cmd = args.hook_cmd
    if cmd in ("pretool", "subagent-start"):
        # Claude Code calls these on every subagent start: quiet, fast,
        # and never a traceback (see hookmode: they fail open).
        text = _read_stdin()
        out, rc = (hookmode.run_pretool(text) if cmd == "pretool"
                   else hookmode.run_subagent_start(text))
        if out:
            print(out)
        return rc
    try:
        if cmd == "install":
            return _hook_install(args)
        if cmd == "uninstall":
            return _hook_uninstall(args)
        return _hook_status()
    except hookmode.HookError as e:
        print(f"shortleash: error: {e}", file=sys.stderr)
        return 2


def _hook_install(args) -> int:
    if args.max_spawns is not None and args.max_spawns < 0:
        print("shortleash: error: --max-spawns must be >= 0", file=sys.stderr)
        return 2
    loop_cfg, loop_flag, bad = _loop_args(args)
    if bad:
        print(f"shortleash: error: {bad}", file=sys.stderr)
        return 2
    path = _hook_settings_file(args)
    base = hookmode.self_command()
    if hookmode.console_script() is None:
        print("shortleash: warning: the `shortleash` command was not found "
              "next to this Python (a source checkout?), so the hooks run "
              "`python -I -m shortleash`. That works only while shortleash "
              "is installed for that Python (`pip install .` in the "
              "checkout); otherwise the hooks do nothing, silently.",
              file=sys.stderr)
    if args.dry_run:
        print(json.dumps({"hooks": {
            event: [entry] for event, entry
            in hookmode.hook_entries(base, bool(loop_flag)).items()}},
            indent=2))
        print(f"\n(dry run: {path} was not changed)")
        return 0
    changed = hookmode.update_settings(
        path, lambda settings: hookmode.merge_hooks(
            settings, base, loop_flag))
    if args.max_spawns is not None:
        hookmode.write_default_cap(args.max_spawns)
    if loop_flag is True:
        hookmode.write_loop_config(loop_cfg)
    elif loop_flag is False:
        hookmode.write_loop_config(None)
    hookmode.prune()
    print(f"{'Added the hooks to' if changed else 'Hooks already in'} {path}"
          + (" (your previous file is kept next to it as .bak)"
             if changed else ""))
    print("  PreToolUse on Agent|Task: refuses a subagent start once a "
          "session is over its limit")
    print("  SubagentStart: notes which kind of subagent started "
          "(`shortleash hook status`)")
    if loop_flag is True:
        c = loopguard_mod.finish([loop_cfg])
        print(f"  Repeat-call guard (PreToolUse on every tool): warn at "
              f"{c['warn'] or 'never'}, refuse at {c['deny'] or 'never'} "
              f"identical calls among the last {c['window']}"
              + (f"; velocity warning above ${c['velocity']:g}/min"
                 if c.get("velocity") else ""))
        print("    It fails open, sees identical calls only, and runs a "
              "process for every tool call.")
    cap, where = hookmode.resolve_cap(os.getcwd())
    if cap is None:
        print("No limit is set yet, so nothing is refused: pass "
              "--max-spawns N, set $SHORTLEASH_MAX_SPAWNS, or put "
              "max_spawns in a .shortleash.toml.")
    else:
        print(f"Limit: {cap} subagent starts per session (from {where}).")
    print("It cannot: stop a subagent that is already running, take back "
          "tokens already spent, limit cost (use `shortleash run` for "
          "that), or work if the command is moved away.")
    return 0


def _loop_args(args):
    """(settings dict for the guard or None, True/False/None = on/off/leave,
    problem text or "")."""
    get = lambda name: getattr(args, name, None)  # noqa: E731 - init has none
    given = {"warn": get("loop_warn"), "deny": get("loop_deny"),
             "window": get("loop_window"),
             "velocity": get("velocity_usd_per_min")}
    for name, value in (("--loop-warn", given["warn"]),
                        ("--loop-deny", given["deny"]),
                        ("--loop-window", given["window"])):
        if value is not None and value < 0:
            return None, None, f"{name} must be >= 0"
    if given["velocity"] is not None and not (given["velocity"] > 0):
        return None, None, "--velocity-usd-per-min must be greater than 0"
    if get("no_loop_guard"):
        if get("loop_guard") or any(v is not None for v in given.values()):
            return None, None, ("--no-loop-guard cannot be combined with "
                                "the other loop options")
        return None, False, ""
    if not (get("loop_guard") or any(v is not None for v in given.values())):
        return None, None, ""
    cfg = {k: v for k, v in given.items() if v is not None}
    full = loopguard_mod.finish([cfg or {"warn": loopguard_mod.DEFAULT_WARN}])
    why = loopguard_mod.problems(full)
    if why:
        return None, None, why[0]
    return cfg or {"warn": loopguard_mod.DEFAULT_WARN}, True, ""


def _hook_uninstall(args) -> int:
    path = _hook_settings_file(args)
    if not os.path.exists(path):
        print(f"{path} does not exist; nothing to remove")
        return 0
    count = [0]

    def change(settings):
        count[0] = hookmode.remove_hooks(settings)
    hookmode.update_settings(path, change)
    print(f"Removed {count[0]} shortleash hook(s) from {path}; "
          f"every other setting was left as it was.")
    return 0


def _hook_status() -> int:
    mine = [(scope, hookmode.settings_path(scope)) for scope in
            ("user", "project", "local")]
    for scope, path in mine:
        events = hookmode.installed_in(path)
        print(f"{scope:8} {path}: "
              + (", ".join(events) if events else "not installed"))
    cap, where = hookmode.resolve_cap(os.getcwd())
    print("limit:   " + (f"{cap} per session (from {where})" if cap is not None
                         else "none set (nothing is refused)"))
    lc = hookmode.resolve_loop(os.getcwd())
    print("repeat-call guard: " + (
        f"warn at {lc['warn'] or 'never'}, refuse at {lc['deny'] or 'never'} "
        f"identical calls among the last {lc['window']}"
        + (f"; velocity above ${lc['velocity']:g}/min" if lc["velocity"]
           else "") if lc else "off"))
    rows = hookmode.sessions()
    if not rows:
        print("no sessions counted yet")
        return 0
    print("recent sessions (newest first):")
    for r in rows:
        kinds = ", ".join(f"{k} x{v}" for k, v in sorted(
            r["agents"].items(), key=lambda kv: -kv[1])[:4]) or "-"
        when = time.strftime("%Y-%m-%d %H:%M", time.localtime(r["last"]))
        print(f"  {r['session'][:12]:12} {when}  asked {r['requests']}, "
              f"refused {r['denied']}, started {r['started']}  [{kinds}]")
    return 0


def _fleet_db() -> str:
    return fleet_mod.DB_PATH or fleet_mod.paths.fleet_db_path()


def _caps_banner(args) -> str:
    """Only the caps that are actually set — never `budget=$None`."""
    caps = []
    if args.budget is not None:
        caps.append(f"budget=${args.budget:g}")
    if args.max_spawns is not None:
        caps.append(f"max_spawns={args.max_spawns}")
    if args.max_tokens is not None:
        caps.append(f"max_tokens={args.max_tokens}")
    if args.timeout is not None:
        caps.append(f"timeout={args.timeout:g}s")
    if args.anomaly is not None:
        caps.append(f"anomaly={args.anomaly:g}x (needs {args.min_history} runs)")
    return " ".join(caps) if caps else "no caps set (nothing will be enforced)"


# Tests patch this to exercise the Windows branch on any OS.
_WINDOWS = os.name == "nt"


def _pipe_gone(exc: BaseException) -> bool:
    """Did the reader of our stdout go away?

    POSIX raises BrokenPipeError (EPIPE). Windows reports the same thing as
    a plain OSError with errno EINVAL and no file name (measured on Windows
    11 / Python 3.11: errno 22, winerror None), raised by the FLUSH rather
    than by the print, and it fails a second time at interpreter exit
    (status 120) unless fd 1 is pointed at the null device first."""
    if isinstance(exc, BrokenPipeError):
        return True
    if not isinstance(exc, OSError):
        return False
    if exc.errno == errno.EPIPE:
        return True
    return (_WINDOWS and exc.errno == errno.EINVAL
            and getattr(exc, "filename", None) is None)


def _stdout_gone() -> None:
    """The reader of our stdout went away (`shortleash cache guide | head`):
    nothing left to say, so say it quietly. Python flushes stdout once more
    at exit, which would raise the same BrokenPipeError again — point the
    descriptor at the null device first."""
    try:
        devnull = os.open(os.devnull, os.O_WRONLY)
        os.dup2(devnull, sys.stdout.fileno())
    except OSError:  # no real descriptor (captured stdout): nothing to do
        pass


def _print_command(fn, args) -> int:
    """Run a subcommand that only prints (cache, fleet): a closed pipe is
    exit 0 and silence, never a traceback. (`run` is not routed through
    here: the leash handles its own stdout, and its exit code is the
    verdict on the agent run — it must not be rewritten to 0.)"""
    try:
        rc = fn(args)
        if sys.stdout is not None:
            sys.stdout.flush()  # a closed pipe surfaces here, not at exit
        return rc
    except OSError as e:
        if not _pipe_gone(e):
            raise
        _stdout_gone()
        return 0


def main(argv: list[str] | None = None) -> int:
    _harden_streams()
    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.cmd == "cache":
        return _print_command(_cmd_cache, args)
    if args.cmd == "fleet":
        return _print_command(_cmd_fleet, args)
    if args.cmd == "hook":
        return _print_command(_cmd_hook, args)
    if args.cmd == "init":
        return _print_command(_cmd_init, args)
    if args.cmd == "report":
        return _print_command(_cmd_report, args)
    if args.cmd == "run":
        merged = _apply_profile(args)
        if merged:
            return merged
        # A cap that is NaN or infinite compares False with everything: it
        # would never trip, yet the banner and the receipt would list it as
        # enforced. Refuse it before anything is spent.
        for flag, value in (("--budget", args.budget),
                            ("--timeout", args.timeout),
                            ("--grace-period", args.grace_period),
                            ("--anomaly", args.anomaly)):
            if value is not None and not math.isfinite(value):
                parser.error(f"{flag} must be a finite number (got {value:g})")
        # Guardrail caps must be non-negative; a negative cap would either
        # trip every run instantly or (worse) disable the check silently.
        for flag, value in (("--budget", args.budget),
                            ("--max-spawns", args.max_spawns),
                            ("--max-tokens", args.max_tokens),
                            ("--grace-period", args.grace_period)):
            if value is not None and value < 0:
                parser.error(f"{flag} must be >= 0 (got {value})")
        # A zero timeout would trip the instant the child starts.
        if args.timeout is not None and args.timeout <= 0:
            parser.error(f"--timeout must be > 0 seconds (got {args.timeout:g})")
        if args.anomaly is not None and args.anomaly <= 0:
            parser.error(f"--anomaly must be > 0 (got {args.anomaly:g})")
        # The baseline is the median of the past runs: there is none to take
        # from zero of them.
        if args.min_history < 1:
            parser.error(f"--min-history must be >= 1 (got {args.min_history})")
        # A claim lives for its TTL and no longer than a day: the database
        # used to cut a larger value down without a word. Say so instead,
        # whether or not --fleet is given (every option is checked up front).
        if not 1 <= args.fleet_ttl <= fleet_mod.MAX_TTL:
            parser.error(f"--fleet-ttl must be between 1 and "
                         f"{fleet_mod.MAX_TTL} seconds (got {args.fleet_ttl})")
        if args.no_anomaly:
            args.anomaly = None
        # argparse puts everything after `--` in `command`, possibly
        # with a leading `--` itself.
        cmd = list(args.command)
        if cmd and cmd[0] == "--":
            cmd = cmd[1:]
        if not cmd:
            parser.error("no command given: shortleash run [options] -- <command>")
        cmd = _maybe_inject_stream_json(cmd)

        try:
            pricing_table = pricing.load_table(args.pricing)
        except (OSError, ValueError) as e:
            # json.JSONDecodeError is a ValueError; never dump a traceback.
            print(f"shortleash: error: can't load pricing file "
                  f"{args.pricing or os.environ.get(pricing.PRICING_ENV)}: "
                  f"{e}", file=sys.stderr)
            return 2

        g = guard_mod.Guard(
            budget=args.budget,
            max_spawns=args.max_spawns,
            max_tokens=args.max_tokens,
            timeout=args.timeout,
            grace_period=args.grace_period,
            pricing_table=pricing_table,
            receipt_path=args.receipt,
            quiet=args.quiet,
            anomaly=args.anomaly,
            profile=args.profile,
            history_path=args.history_path,
            min_history=args.min_history,
            redact_prompt=args.redact_prompt,
            forecast=not args.no_forecast,
        )
        if not args.quiet:
            # Never echo the full argv: it may contain secrets. Show only
            # the agent binary name alongside the caps.
            agent_bin = guard_mod.agent_name(cmd[0])
            source = (f"[shortleash] limits from {args.profile_file}\n"
                      if args.profile_file else "")
            _stderr_write(
                f"[shortleash] guarding: {agent_bin}\n"
                f"[shortleash] {_caps_banner(args)}\n"
                f"{source}"
                f"[shortleash] pricing: {pricing_table.describe()}\n"
            )
            stale = pricing.staleness(pricing_table.as_of)
            if stale:
                _stderr_write(f"[shortleash] WARNING: {stale}\n")
        if args.fleet:
            return _run_with_fleet(g, cmd, args)
        return g.run(cmd)
    return 0


def _stderr_write(text: str) -> None:
    """Advisory output: a closed or broken stderr never stops the run."""
    try:
        sys.stderr.write(text)
        sys.stderr.flush()
    except (OSError, ValueError, AttributeError):
        pass


def _run_with_fleet(g, cmd: list[str], args) -> int:
    """Hold a fleet claim for the run. Warn-don't-block on conflict;
    release on exit. The Guard itself turns Ctrl-C/SIGTERM into a clean
    return (tree killed, receipt written), so the release runs in the
    `finally`; atexit and the claim TTL back it up for a hard kill."""
    paths = args.claim or [os.getcwd()]
    try:
        conn = fleet_mod.connect()
        try:
            res = fleet_mod.claim(
                conn, paths, owner=args.fleet_owner,
                note=f"shortleash run: {guard_mod.agent_name(cmd[0])}",
                ttl=args.fleet_ttl)
        finally:
            conn.close()
    except ValueError as e:
        print(f"shortleash: fleet claim error: {e}", file=sys.stderr)
        return 2
    except fleet_mod.FleetUnavailable as e:
        # Also advisory: no sqlite3 in this Python is a warning, not a stop.
        note = f"{e}; running WITHOUT a claim"
        _stderr_write(f"[shortleash] fleet WARNING: {note}\n")
        g.warnings.append(note)
        return g.run(cmd)
    except fleet_mod.db_errors() as e:
        # A claim is advisory (a conflict only warns): a database that
        # cannot be opened is a warning too, never a reason not to run.
        note = (f"fleet database unusable ({_fleet_db()}): {e}; running "
                f"WITHOUT a claim")
        _stderr_write(f"[shortleash] fleet WARNING: {note}\n")
        g.warnings.append(note)
        return g.run(cmd)
    claim_id = res.get("claim_id")

    def _release() -> None:
        if claim_id:
            try:
                c2 = fleet_mod.connect()
                fleet_mod.release(c2, claim_id)
                c2.close()
            except Exception:
                pass

    # Release is registered and the try is entered BEFORE anything else can
    # fail: a stderr that closed early (`2>&1 | head -1`) used to raise out
    # of the writes below, leaking the claim and never starting the agent.
    atexit.register(_release)
    try:
        if res["ok"]:
            if not args.quiet:
                _stderr_write(
                    f"[shortleash] fleet claim {claim_id}: "
                    f"{', '.join(res['paths'])}\n")
        else:
            _stderr_write("[shortleash] fleet WARNING: paths already "
                          "claimed \u2014 proceeding anyway:\n")
            for c in res["conflicts"]:
                h = c["holder"]
                _stderr_write(
                    f"  {c['path']} held by {h['owner'] or '?'} "
                    f"({h['ttl_left']}s left)"
                    + (f": {h['note']}" if h["note"] else "") + "\n")
        return g.run(cmd)
    finally:
        _release()


if __name__ == "__main__":
    sys.exit(main())
