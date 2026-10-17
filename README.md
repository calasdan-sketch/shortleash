# ShortLeash — stop runaway agents

A circuit breaker for AI coding-agent runs. Wraps an agent command, tails its
telemetry in real time, and **kills the run the moment it breaches a guardrail**
— budget, subagent-spawn cap, token cap, or wall-clock timeout. Emits a
per-run receipt showing what it cost and why it stopped.

Vendors' "budgets" only warn while the meter keeps running. This one halts.

## Install

Zero dependencies — Python 3.10+ stdlib only.

```bash
git clone <repo> && cd shortleash
# run directly:
python3 -m shortleash run --budget 2.00 -- <command>
# or add an alias (from the repo root, no install):
alias shortleash="python3 -m shortleash"
# or install for the `shortleash` console script:
pip install .
```

Works on Linux, macOS and Windows 11 (Python 3.10+). On Windows, `claude`
is found through `PATHEXT` (the npm `claude.cmd` shim), and console output
never dies on a cp1252 code page — unencodable characters are replaced.

## Usage

```bash
# Guard a Claude Code run: kill it past $2 or 10 subagent spawns.
shortleash run --budget 2.00 --max-spawns 10 -- \
  claude -p "refactor the auth module"

# All guardrails:
shortleash run --budget 5.00 --max-spawns 10 --max-tokens 500000 \
  --timeout 600 --receipt ./receipt.json -- \
  claude -p "migrate the database layer"
```

### Anomaly mode (the smart default)

Static caps punish legitimate big tasks. Anomaly mode learns instead:
every receipt is appended to `~/.shortleash/history.jsonl` (or under
`$SHORTLEASH_HOME`), and once an agent has 5+ past runs, the guard trips only when the live run exceeds
**3× the historical median** cost or tokens for that agent.

```bash
shortleash run -- claude -p "migrate the database layer"
# [shortleash] anomaly=3.0x (needs 5 runs)
# ...
# STOPPED: anomaly: estimated $4.21 is 5.3x the typical $0.79 across 23 past runs
```

A naturally large task establishes its own baseline over time; only true
runaways trip. Breached runs are excluded from baselines. Tune with
`--anomaly 5.0`, `--min-history 10`, `--profile big-refactor` (separate
baseline), or `--no-anomaly` to disable.

### Fleet mode (`--fleet`)

Running several agents on the same machine? `--fleet` holds a **fleet
claim** on paths for the run — a lockfile for agents, merged from the
retired claimlock:

```bash
shortleash run --fleet --budget 3 -- claude -p "refactor the api layer"
# [shortleash] fleet claim ce63a732a87b: /home/dan/project
```

- Default claim: the current directory; override with
  `--claim /path/one /path/two`.
- **Conflict = warning, never a block.** If another agent holds an
  overlapping path, you get a loud stderr warning and the run proceeds
  anyway — a lock must never be a deadlock.
- Released automatically on exit (normal, signal, or crash via the TTL
  backstop). Tune with `--fleet-owner "dan"` and `--fleet-ttl 3600`.

### Prompt cache optimizer

Stop the bleed *and* shrink the bill. Prompt caching discounts repeated
prefix tokens (up to 90% off) — but only when stable content sits first
and per-request variables sit last:

```bash
shortleash cache guide                  # provider docs + the core rule
shortleash cache check -i prompt.txt    # how cacheable is this prompt?
shortleash cache make -i prompt.txt -o prompt.cached.txt --provider anthropic
```

`cache make` restructures your `{{variable}}`-templated prompt into
cache-optimal layout and emits integration snippets for Anthropic (explicit
`cache_control` breakpoints), OpenAI/DeepSeek (automatic prefix caching),
and Gemini (explicit context caching).

When wrapping `claude`, `--output-format stream-json --verbose` are added
automatically (the guard needs the telemetry stream). The agent's stdout
passes through untouched, so you still watch it work.

On breach the **whole process tree** is killed. On POSIX the child runs in
its own process group (`start_new_session`): SIGTERM, then SIGKILL after
`--grace-period` (default 5s). On Windows the child is placed in a
kill-on-close **Job Object** the moment it starts, so everything it spawns
is in the job too and one `TerminateJobObject` ends the tree — orphans
whose parent already exited included (no SIGTERM there, so the grace
period does not apply to the kill itself). If the job cannot be set up,
the fallback is `taskkill /T /F` plus an orphan sweep by parent pid.

Children that outlive the command are handled too. If the agent exits but
leaves subprocesses running (which also keep its output pipe open), the
leash waits one grace period for the pipe to close, then kills the
stragglers by process group and says so in the receipt — it never hangs on
them. A grandchild that detached into its own session is out of reach by
design; the receipt warns about it instead of waiting forever.

Exit codes: `124` on a guard kill; `125` when a budget/token/spawn/anomaly
cap was requested but the child produced **no usage telemetry** (the cap
could not have been enforced — see *Trust boundary*); otherwise the child's
exit code.

## The receipt

Every run ends with a receipt on stderr and optionally as JSON
(`--receipt path.json`):

```
── shortleash receipt ──────────────────────
  cost:      $0.1271 (estimated)
  tokens:    51,000 (in=15,000 out=4,500 cache_r=30,000 cache_w=1,500)
  turns:     0   spawns: 1
  duration:  0.16s   exit: 124
  STOPPED:   budget exceeded: estimated $0.1271 > $0.05 cap
──────────────────────────────────────────────────
```

Mid-run cost is estimated from a fallback pricing table (clearly marked);
the final receipt prefers the agent-reported `total_cost_usd` when the run
completes. The receipt JSON also carries `telemetry_seen`, `usage_events`,
`enforced` / `not_enforced` (which caps actually held), `pricing`
(`as_of`, `source`) and `warnings` (e.g. stray child processes).

### Prices are a hard-coded fallback — verify them

The built-in table (`pricing.py`, USD per 1M tokens) is **not fetched from
anywhere**; it was written on the date in `PRICING_AS_OF` and is printed in
the startup banner and the receipt as
`pricing: built-in fallback table as of 2026-10-16 — verify against
provider pricing`. Check it against the provider's pricing page before
trusting a `--budget` to the cent, and override it with `--pricing
pricing.json` or the `SHORTLEASH_PRICING` environment variable pointing at a
JSON file:

```json
{"as_of": "2026-10-16",
 "sonnet": [3.0, 15.0], "opus": [15.0, 75.0], "haiku": [0.8, 4.0]}
```

`as_of` is optional metadata echoed in the receipt; every other key is a
case-insensitive substring matched against the model name, mapped to
`[input, output]` dollars per 1M tokens.

### No telemetry, no comfort

Wrap something that never emits stream-json usage events (a plain script,
`claude` without `-p`, a different agent) and the guard has nothing to
enforce a cost cap against. It says so instead of pretending:

```
  cost:      n/a (no usage data seen)
  ...
  WARNING:   no usage data seen — only the wall-clock/time limit was enforced; NOT enforced: budget
```

The exit code is `125` in that case (the run itself may have succeeded,
but the cap you asked for never held), the cost is `null` rather than
`$0.00`, and the run is not added to the anomaly history.

## How it works

- Child runs in its own process group on POSIX (`start_new_session=True`)
  and in a kill-on-close Job Object on Windows (`kernel32` via ctypes,
  `taskkill /T /F` as the fallback; see `proctree.py`) — a kill takes the
  whole tree, never your shell.
- A monitor thread parses each stream-json line (`system/init`,
  `assistant` usage + `Task` tool_use spawns, `result` cost) and checks caps
  after every observation.
- A watchdog thread enforces `--timeout` even if the child goes silent.
- Subagent spawns are counted from `Task` tool_use blocks in the stream.

## Trust boundary

Be honest about what the guard can and can't see:

- **Token, budget, spawn, and anomaly caps all depend on the child
  agent's own telemetry.** Token counts come from its stream-json
  `usage` events; mid-run cost is estimated from those counts; the final
  receipt prefers the child's reported `total_cost_usd`.
- **A misbehaving or compromised child that under-reports (or reports
  negative) usage can blind those caps.** The guard clamps every
  child-reported usage and cost value to ≥ 0 so negatives can't drive
  totals *down*, but it cannot see spend the child simply hides.
- **Only `--timeout` is enforced independently of the child** — a
  wall-clock watchdog, immune to telemetry lies.
- **No telemetry at all means no cost cap was enforced.** The receipt
  then says `no usage data seen — only the wall-clock/time limit was
  enforced`, lists the caps that were NOT enforced, reports the cost as
  n/a, and exits `125` if such a cap was requested.
- **Prices are a dated fallback table**, not live data: every receipt
  prints its `as_of` date — verify against provider pricing.

Treat this as a guardrail against runaway cost, not a security boundary
against an adversarial agent.

## Testing

```bash
python3 -m unittest discover -s tests -v   # or: python3 -m pytest -q
```

`sim/runaway.py` is a fake agent emitting realistic stream-json — the suite
kills it for real (budget, spawn, token, and timeout breaches) without
spending a cent. It also spawns escaped grandchildren to prove the leash
returns and kills the whole tree, and it never touches your real
`~/.shortleash` (state lives under `SHORTLEASH_HOME`, which the tests point
at a temp dir).

## Scope / limits (v0.1.0)

- Claude Code `-p` (non-interactive) is the supported path. Interactive
  sessions and other agents (Codex, Cursor) are on the roadmap — the
  event parser is isolated in `events.py` for exactly this.
- Cost mid-run is an estimate; treat `--budget` as a guardrail, not accounting.
