# ShortLeash — stop runaway agents

A circuit breaker for AI coding-agent runs. Wraps an agent command, tails its
telemetry in real time, and **kills the run the moment it breaches a guardrail**
— budget, subagent-spawn cap, token cap, or wall-clock timeout. Emits a
per-run receipt showing what it cost and why it stopped.

Vendors' "budgets" only warn while the meter keeps running. This one halts.

## Install

Zero dependencies — Python 3.10+ stdlib only.

```bash
pip install git+https://github.com/calasdan-sketch/shortleash
shortleash run --budget 2.00 -- <command>

# or, from a checkout, without installing:
python3 -m shortleash run --budget 2.00 -- <command>
```

Works on Linux, macOS and Windows 11 (Python 3.10+). On Windows, `claude`
is found through `PATHEXT` (the npm `claude.cmd` shim — see *Windows*
below), and console output never dies on a cp1252 code page — unencodable
characters are replaced.

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

Only the caps you set are enforced; the banner lists them (or says
`no caps set`). `--timeout` must be greater than zero.

When wrapping `claude`, `--output-format stream-json --verbose` are added
automatically (the guard needs the telemetry stream). The agent's stdout
passes through untouched, so you still watch it work — and if whatever is
reading it goes away (`shortleash run … | head -1`), the echo stops but the
caps keep being checked; the receipt notes it. Non-JSON lines on that
stdout (warnings, entrypoint chatter) are not telemetry and are ignored; a
telemetry event whose fields are malformed (`"input_tokens": "abc"`) trips
the run — the guard fails closed rather than guess.

`--receipt path.json` creates a missing directory, and a path that cannot
be written to at all is refused *before* the agent starts (exit `2`) — not
discovered after the money was spent. Should the write still fail at the
end (disk full), the receipt summary is printed on stderr instead, with a
warning, even under `--quiet`.

### Stopping the leash (Ctrl-C, SIGTERM)

The agent runs in its own process group, so a Ctrl-C in your terminal (or a
`kill` from a supervisor) reaches **only the leash**. The leash then kills
the agent's whole process tree, writes the receipt (`STOPPED: interrupted
(Ctrl-C)` / `terminated (SIGTERM)`) and exits `130` / `143` — it never dies
with a traceback leaving the agent running. A second Ctrl-C during the
grace period force-kills at once. This holds at every point of the run,
including the window after the agent itself has exited while the leash
waits for stray processes it left behind (see *Killing the tree*): a signal
there kills them at once, with no grace, and the receipt says `STOPPED:
interrupted (Ctrl-C): the leash was signalled after the command exited and
killed the process(es) it left running`. On Windows, Ctrl-Break is handled
like SIGTERM.

### Anomaly mode (`--anomaly`, opt-in)

Static caps punish legitimate big tasks. Anomaly mode learns instead:
every run with telemetry is appended to `~/.shortleash/history.jsonl` (or
under `$SHORTLEASH_HOME`; no command text or prompt is stored there), and
with `--anomaly` the guard trips once an agent has 5+ past runs and the
live run exceeds **3× the historical median** cost or tokens for that agent.

```bash
shortleash run --anomaly -- claude -p "migrate the database layer"
# [shortleash] anomaly=3x (needs 5 runs)
# ...
# STOPPED:   anomaly: estimated $4.21 is 5.3x the typical $0.79 across 23 past runs
# hint:      stopped by anomaly mode, so this run is NOT in the baseline and would
#            be stopped again: re-run without --anomaly to let it finish and be
#            recorded, or use --profile NAME for a separate baseline
```

Know the ratchet: **breached runs are excluded from the baseline**, so a
legitimately larger task that anomaly mode stops does *not* teach the
baseline anything — it would be stopped again on every attempt. That is
why anomaly mode is off unless you ask for it, and why the STOPPED line
tells you the way out: run the task once without `--anomaly` (it is then
recorded and the median moves), or give it its own baseline with
`--profile big-refactor`. Tune with `--anomaly 5.0` and `--min-history 10`.
`--no-anomaly` is still accepted (it is the default). Until the history is
long enough, anomaly mode is **not armed** and enforces nothing: the
receipt says so (`anomaly:   not armed (2 of 5 runs) — nothing was enforced
by it`, a `warning:` line, and `"anomaly": {"armed": false, "runs": 2,
"min_runs": 5, …}` in the JSON) and never lists `anomaly` under
`enforced`.

### Fleet mode (`--fleet`)

Running several agents on the same machine? `--fleet` holds a **fleet
claim** on paths for the run — a lockfile for agents, merged from the
retired claimlock:

```bash
shortleash run --fleet --budget 3 -- claude -p "refactor the api layer"
# [shortleash] fleet claim ce63a732a87b: /home/dan/project
shortleash fleet list
# ce63a732a87b  /home/dan/project  held by dan  (1791s left)  — shortleash run: claude
```

- Default claim: the current directory; override with
  `--claim /path/one /path/two`.
- **Conflict = warning, never a block.** If another agent holds an
  overlapping path, you get a loud stderr warning and the run proceeds
  anyway — a lock must never be a deadlock.
- Released automatically on exit (normal, Ctrl-C/SIGTERM, or crash via the
  TTL backstop). Tune with `--fleet-owner "dan"` and `--fleet-ttl 3600`.
- `shortleash fleet list` shows who holds what right now.

### Killing the tree

On breach the **whole process tree** is killed. On POSIX the child runs in
its own process group (`start_new_session`): SIGTERM, then SIGKILL after
`--grace-period` (default 5s). On Windows the child is placed in a
kill-on-close **Job Object** immediately after launch, so everything it
spawns from then on is in the job too and one `TerminateJobObject` ends the
tree — orphans whose parent already exited included (no SIGTERM there, so
the grace period does not apply to the kill itself). A grandchild spawned
in the instant between `CreateProcess` and the job assignment is not
covered. If the job cannot be set up, the fallback is `taskkill /T /F` plus
an orphan sweep by parent pid.

Children that outlive the command are handled too. If the agent exits but
leaves subprocesses running (which also keep its output pipe open), the
leash waits one grace period for the pipe to close, then kills the
stragglers by process group and says so in the receipt — it never hangs on
them. A grandchild that detached into its own session is out of reach by
design; the receipt warns about it instead of waiting forever.

### Exit codes

| code | meaning |
|---|---|
| `124` | a cap tripped and the tree was killed (mirrors `timeout(1)`) |
| `125` | a budget/token/spawn/anomaly cap was requested but the child produced **no usage telemetry** — the cap could not have been enforced (see *Trust boundary*) |
| `130` / `143` | the leash itself got Ctrl-C / SIGTERM (Ctrl-Break on Windows) — at any point, the wait for stray children after the agent exited included; tree killed, receipt written |
| `1` | the process exited 0 but its final stream-json `result` said `is_error: true` (`FAILED:` in the receipt, e.g. "Not logged in") |
| `128+N` | POSIX: the child was killed by signal N outside any breach (what a shell would report) |
| `2` | usage error, command not found, unsafe `.cmd` launch refused, receipt path not writable |
| other | the child's own exit code (on Windows its 32-bit status, clamped to 0–255; the raw value is `child_returncode` in the receipt) |

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
`enforced` / `not_enforced` (which caps actually held), `anomaly` (whether
anomaly mode was armed, and on how many runs), `pricing` (`as_of`,
`source`), `agent_error` (the `is_error` verdict), `child_returncode` (what
the child itself returned, untouched), `hint` and `warnings` (stray child
processes, a closed stdout, corrupt history lines, an un-armed anomaly cap,
a receipt that could not be written).

### What the receipt stores about your command

`command` is the wrapped argv with **secret-looking values redacted**: the
value after any flag whose name contains `key`, `token`, `secret`,
`password` or `passwd` (`--api-key sk-…`, `--token=…`, `DB_PASSWORD=…`)
becomes `***`. **The prompt is not a secret by that rule and is stored
verbatim** — `-p "my password is hunter2"` ends up in `receipt.json` as
typed. Pass `--redact-prompt` to replace the argument after `-p`,
`--print` or `--prompt` (and a `--prompt=…` value) with `<redacted>`; a
prompt given any other way (bare positional, stdin, a file) is not
recognised. The history file never stores command text.

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
  `assistant` usage + subagent `tool_use` spawns, `result` cost/`is_error`)
  and checks caps after every observation.
- Claude Code emits **one `assistant` event per content block**, all with
  the same `message.id` and the same `usage` (its docs say so). Usage is
  counted once per message id — a block that arrives with a larger
  `output_tokens` tops the count up, it is never added twice — so the
  budget trips on real spend, not on duplicates. Events without a message
  id (other emitters) are each counted.
- Subagent spawns are counted from `tool_use` blocks named `Agent`
  (current Claude Code) or `Task` (older versions), once per block id.
  Subagents' own messages are in the stream too and their usage counts —
  it is real spend.
- A watchdog thread enforces `--timeout` even if the child goes silent.

## Windows

- `claude` from npm is a `claude.cmd` shim, and Windows runs `.cmd`/`.bat`
  files through **cmd.exe**, which re-parses the command line: `%VAR%` is
  expanded, `^` is eaten, an embedded `"` can let `&`, `|`, `<`, `>` run
  other commands (the BatBadBut class of argument injection) — a prompt
  taken from a file or a ticket is exactly that kind of text. The leash
  therefore reads the npm shim and launches what it would launch,
  `node …\node_modules\@anthropic-ai\claude-code\cli.js`, directly, with no
  cmd.exe in between. Any other `.cmd`/`.bat` target is refused (exit `2`,
  clear message) when an argument contains `"`, `%`, `!`, `^`, `&`, `|`,
  `<`, `>` or a newline; plain arguments still run as before. A `claude.exe`
  (native installer) needs none of this.
- The child is started with `CREATE_NEW_PROCESS_GROUP`, which makes it
  **ignore console Ctrl-C** — so the leash handles Ctrl-C itself and kills
  the Job (see *Stopping the leash*). The leash's wait is polled in short
  slices so Ctrl-C is noticed promptly on Windows too.
- If the leash dies hard (task manager), the kill-on-close Job Object
  takes the agent down with it.

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

## Prompt cache optimizer (`shortleash cache`) — a separate helper

This lives in the package but has nothing to do with the leash, the
receipt or the history; it may move to its own package later. Prompt
caching discounts repeated prefix tokens (up to 90% off) — but only when
stable content sits first and per-request variables sit last:

```bash
shortleash cache guide                  # provider docs + the core rule
shortleash cache check -i prompt.txt    # how cacheable is this prompt?
shortleash cache make -i prompt.txt -o prompt.cached.txt --provider anthropic
```

`cache make` restructures your `{{variable}}`-templated prompt into
cache-optimal layout and emits integration snippets for Anthropic (explicit
`cache_control` breakpoints), OpenAI/DeepSeek (automatic prefix caching),
and Gemini (explicit context caching).

## Testing

```bash
python3 -m unittest discover -s tests -v   # or: python3 -m pytest -q
```

`sim/runaway.py` is a fake agent emitting realistic stream-json — the suite
kills it for real (budget, spawn, token, and timeout breaches) without
spending a cent. It also spawns escaped grandchildren to prove the leash
returns and kills the whole tree, sends the leash itself Ctrl-C and SIGTERM
(POSIX), closes its stdout mid-run, and never touches your real
`~/.shortleash` (state lives under `SHORTLEASH_HOME`, which the tests point
at a temp dir). The Windows shim resolution and the cmd.exe refusal list
are unit-tested with a fake shim directory on every platform.

## Scope / limits (v0.1.0)

- Claude Code `-p` (non-interactive) is the supported path. Interactive
  sessions and other agents (Codex, Cursor) are on the roadmap — the
  event parser is isolated in `events.py` for exactly this.
- Cost mid-run is an estimate; treat `--budget` as a guardrail, not accounting.
