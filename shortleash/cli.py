"""Stop Runaway Agents — a circuit breaker for AI coding-agent runs.

Wraps an agent command, tails its stream-json telemetry in real time,
and kills the run the moment it breaches a guardrail — budget, spawn
cap, token cap, or wall-clock timeout. Emits a per-run receipt.
"""

import argparse
import atexit
import os
import signal
import sys

from . import cacheopt
from . import fleet as fleet_mod
from . import guard as guard_mod
from . import pricing
from . import __version__


def _harden_streams() -> None:
    """Never crash on a console that cannot encode the receipt or the
    agent's output (Windows cp1252 consoles, piped output under odd
    locales). Python's stderr already replaces unencodable characters;
    make stdout do the same instead of raising UnicodeEncodeError."""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None or getattr(stream, "errors", None) != "strict":
            continue
        try:
            reconfigure(errors="replace")
        except (OSError, ValueError):
            pass


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

    r = sub.add_parser("run", help="Run a command under guardrails.")
    r.add_argument("--budget", type=float, default=None,
                   help="Max estimated spend in USD before the run is killed.")
    r.add_argument("--max-spawns", type=int, default=None,
                   help="Max subagent spawns before the run is killed.")
    r.add_argument("--max-tokens", type=int, default=None,
                   help="Max total tokens (in+out+cache) before the run is killed.")
    r.add_argument("--timeout", type=float, default=None,
                   help="Max wall-clock seconds before the run is killed.")
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
    r.add_argument("--quiet", action="store_true",
                   help="Suppress the human-readable receipt on stderr.")
    r.add_argument("--anomaly", type=float, default=3.0, metavar="MULT",
                   help="Trip when live cost/tokens exceed MULT x the historical "
                        "median for this agent (default 3.0). Needs --min-history "
                        "past runs to arm.")
    r.add_argument("--no-anomaly", action="store_true",
                   help="Disable historical-baseline anomaly detection.")
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

    c = sub.add_parser("cache", help="Prompt-caching optimizer.")
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


def main(argv: list[str] | None = None) -> int:
    _harden_streams()
    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.cmd == "cache":
        return _cmd_cache(args)
    if args.cmd == "run":
        # Guardrail caps must be non-negative; a negative cap would either
        # trip every run instantly or (worse) disable the check silently.
        for flag, value in (("--budget", args.budget),
                            ("--max-spawns", args.max_spawns),
                            ("--max-tokens", args.max_tokens),
                            ("--timeout", args.timeout)):
            if value is not None and value < 0:
                parser.error(f"{flag} must be >= 0 (got {value})")
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
            anomaly=None if args.no_anomaly else args.anomaly,
            profile=args.profile,
            history_path=args.history_path,
            min_history=args.min_history,
        )
        caps = [f"budget=${args.budget}", f"max_spawns={args.max_spawns}",
                f"max_tokens={args.max_tokens}", f"timeout={args.timeout}"]
        if not args.no_anomaly:
            caps.append(f"anomaly={args.anomaly}x (needs {args.min_history} runs)")
        if not args.quiet:
            # Never echo the full argv: it may contain secrets. Show only
            # the agent binary name alongside the caps.
            agent_bin = guard_mod.agent_name(cmd[0])
            sys.stderr.write(
                f"[shortleash] guarding: {agent_bin}\n"
                f"[shortleash] {' '.join(caps)}\n"
                f"[shortleash] pricing: {pricing_table.describe()}\n"
            )
        if args.fleet:
            return _run_with_fleet(g, cmd, args)
        return g.run(cmd)
    return 0


def _run_with_fleet(g, cmd: list[str], args) -> int:
    """Hold a fleet claim for the run. Warn-don't-block on conflict;
    release on exit via finally + atexit + signal handlers, with the
    claim TTL as the backstop for SIGKILL."""
    paths = args.claim or [os.getcwd()]
    conn = fleet_mod.connect()
    try:
        res = fleet_mod.claim(conn, paths, owner=args.fleet_owner,
                              note=f"shortleash run: {guard_mod.agent_name(cmd[0])}",
                              ttl=args.fleet_ttl)
    except ValueError as e:
        print(f"shortleash: fleet claim error: {e}", file=sys.stderr)
        return 2
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
    old_term = signal.getsignal(signal.SIGTERM)
    old_int = signal.getsignal(signal.SIGINT)

    def _on_signal(signum, frame):
        _release()
        signal.signal(signum, signal.SIG_DFL)
        os.kill(os.getpid(), signum)

    signal.signal(signal.SIGTERM, _on_signal)
    signal.signal(signal.SIGINT, _on_signal)
    try:
        return g.run(cmd)
    finally:
        signal.signal(signal.SIGTERM, old_term)
        signal.signal(signal.SIGINT, old_int)
        _release()


if __name__ == "__main__":
    sys.exit(main())
