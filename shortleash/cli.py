"""Stop Runaway Agents — a circuit breaker for AI coding-agent runs.

Wraps an agent command, tails its stream-json telemetry in real time,
and kills the run the moment it breaches a guardrail — budget, spawn
cap, token cap, or wall-clock timeout. Emits a per-run receipt.

Subcommands:
  run    the leash (this tool's purpose)
  fleet  see who holds a fleet claim (`fleet list`)
  cache  a separate helper: the prompt-caching optimizer (cacheopt.py).
         It shares nothing with the leash but the package; see the README
         section of the same name.
"""

import argparse
import errno
import atexit
import os
import sys

from . import cacheopt
from . import fleet as fleet_mod
from . import guard as guard_mod
from . import pricing
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
                   help="Max estimated spend in USD before the run is killed.")
    r.add_argument("--max-spawns", type=int, default=None,
                   help="Max subagent spawns before the run is killed.")
    r.add_argument("--max-tokens", type=int, default=None,
                   help="Max total tokens (in+out+cache) before the run is killed.")
    r.add_argument("--timeout", type=float, default=None,
                   help="Max wall-clock seconds before the run is killed "
                        "(must be > 0).")
    r.add_argument("--grace-period", type=float, default=5.0,
                   help="Seconds between SIGTERM and SIGKILL on breach "
                        "(POSIX; Windows force-kills the tree at once), "
                        "and how long to wait for stray child processes "
                        "after the command exits.")
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
                   help="Suppress the human-readable receipt on stderr.")
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
                   help="Baseline grouping key (default: agent binary name).")
    r.add_argument("--history-path", type=str, default=None,
                   help="History file (default ~/.shortleash/history.jsonl).")
    r.add_argument("--min-history", type=int, default=5,
                   help="Past runs needed before anomaly mode arms.")
    r.add_argument("--fleet", action="store_true",
                   help="Hold a fleet claim on paths for the run (merged "
                        "from claimlock): warns on conflict, releases on "
                        "exit. Default claim: the current directory.")
    r.add_argument("--claim", nargs="+", default=None, metavar="PATH",
                   help="Paths to claim with --fleet (default: cwd).")
    r.add_argument("--fleet-owner", default="",
                   help="Owner label on the fleet claim.")
    r.add_argument("--fleet-ttl", type=int, default=fleet_mod.DEFAULT_TTL,
                   help="Claim TTL seconds (default 1800, max 86400).")
    r.add_argument("command", nargs=argparse.REMAINDER,
                   help="Command to run, after -- : shortleash run --budget 2 -- claude -p ...")


def _add_fleet_parser(sub) -> None:
    f = sub.add_parser("fleet", help="Fleet claims held on this machine.")
    fsub = f.add_subparsers(dest="fleet_cmd", required=True)
    fsub.add_parser("list", help="Show who currently holds which path.")


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
    _add_fleet_parser(sub)
    _add_cache_parser(sub)
    return p


def _maybe_inject_stream_json(argv: list[str]) -> list[str]:
    """If wrapping `claude` without stream-json flags, add them.

    The guard needs the telemetry stream to enforce caps. This keeps the
    common case to: shortleash run --budget 2 -- claude -p "do thing"
    """
    if not argv:
        return argv
    prog = guard_mod.agent_name(argv[0])
    if prog == "claude" and "--output-format" not in argv:
        return argv + ["--output-format", "stream-json", "--verbose"]
    return argv


def _cmd_cache(args) -> int:
    if args.cache_cmd == "guide":
        print(cacheopt.guide_text())
        return 0
    try:
        with open(args.in_file, encoding="utf-8") as f:
            text = f.read()
    except OSError as e:
        print(f"error: can't read {args.in_file}: {e}", file=sys.stderr)
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
        with open(args.out_file, "w", encoding="utf-8") as f:
            f.write(optimized)
        print(rendered)
        print(f"\n  optimized prompt written to {args.out_file}")
    else:
        print(rendered)
    return 0


def _cmd_fleet(args) -> int:
    conn = fleet_mod.connect()
    try:
        claims = fleet_mod.active_claims(conn)
    finally:
        conn.close()
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
    if args.cmd == "run":
        # Guardrail caps must be non-negative; a negative cap would either
        # trip every run instantly or (worse) disable the check silently.
        for flag, value in (("--budget", args.budget),
                            ("--max-spawns", args.max_spawns),
                            ("--max-tokens", args.max_tokens)):
            if value is not None and value < 0:
                parser.error(f"{flag} must be >= 0 (got {value})")
        # A zero timeout would trip the instant the child starts.
        if args.timeout is not None and args.timeout <= 0:
            parser.error(f"--timeout must be > 0 seconds (got {args.timeout:g})")
        if args.anomaly is not None and args.anomaly <= 0:
            parser.error(f"--anomaly must be > 0 (got {args.anomaly:g})")
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
        )
        if not args.quiet:
            # Never echo the full argv: it may contain secrets. Show only
            # the agent binary name alongside the caps.
            agent_bin = guard_mod.agent_name(cmd[0])
            sys.stderr.write(
                f"[shortleash] guarding: {agent_bin}\n"
                f"[shortleash] {_caps_banner(args)}\n"
                f"[shortleash] pricing: {pricing_table.describe()}\n"
            )
        if args.fleet:
            return _run_with_fleet(g, cmd, args)
        return g.run(cmd)
    return 0


def _run_with_fleet(g, cmd: list[str], args) -> int:
    """Hold a fleet claim for the run. Warn-don't-block on conflict;
    release on exit. The Guard itself turns Ctrl-C/SIGTERM into a clean
    return (tree killed, receipt written), so the release runs in the
    `finally`; atexit and the claim TTL back it up for a hard kill."""
    paths = args.claim or [os.getcwd()]
    conn = fleet_mod.connect()
    try:
        res = fleet_mod.claim(conn, paths, owner=args.fleet_owner,
                              note=f"shortleash run: {guard_mod.agent_name(cmd[0])}",
                              ttl=args.fleet_ttl)
    except ValueError as e:
        print(f"shortleash: fleet claim error: {e}", file=sys.stderr)
        return 2
    finally:
        conn.close()
    claim_id = res.get("claim_id")

    def _release() -> None:
        if claim_id:
            try:
                c2 = fleet_mod.connect()
                fleet_mod.release(c2, claim_id)
                c2.close()
            except Exception:
                pass

    if res["ok"]:
        if not args.quiet:
            sys.stderr.write(
                f"[shortleash] fleet claim {claim_id}: "
                f"{', '.join(res['paths'])}\n")
    else:
        sys.stderr.write("[shortleash] fleet WARNING: paths already "
                         "claimed — proceeding anyway:\n")
        for c in res["conflicts"]:
            h = c["holder"]
            sys.stderr.write(
                f"  {c['path']} held by {h['owner'] or '?'} "
                f"({h['ttl_left']}s left)"
                + (f": {h['note']}" if h["note"] else "") + "\n")

    atexit.register(_release)
    try:
        return g.run(cmd)
    finally:
        _release()


if __name__ == "__main__":
    sys.exit(main())
