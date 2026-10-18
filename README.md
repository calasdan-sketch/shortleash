# ShortLeash — stop runaway agents

A circuit breaker for AI coding-agent runs. Wraps an agent command, tails its
telemetry in real time, and **kills the run the moment it breaches a guardrail**
— budget, subagent-spawn cap, token cap, or wall-clock timeout. Emits a
per-run receipt showing what it cost and why it stopped. "The moment" needs
an agent that streams its usage; an agent that only reports at the end can be
checked, but no longer stopped (see *Live, or only afterwards*).

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
`no caps set`). `--timeout` must be greater than zero, and `--budget`,
`--timeout`, `--grace-period` and `--anomaly` must be finite numbers: `nan`
and `inf` are refused with exit `2` before anything runs, because a cap that
can never trip must not be listed as enforced (before 0.1.1 `--budget nan`
ran the agent unguarded and the receipt called the budget enforced).

When wrapping `claude`, `--output-format stream-json --verbose` are added
automatically (the guard needs the telemetry stream). The agent's stdout
passes through untouched, so you still watch it work — and if whatever is
reading it goes away (`shortleash run … | head -1`), the echo stops but the
caps keep being checked; the receipt notes it. A reader that merely
*stalls* (a paused pager, a pipe nobody drains) cannot blind the caps
either: every line is parsed first and handed to the echo through a bounded
queue (4 MB) on a thread of its own. When the queue is full lines are
dropped, never waited for, and the receipt counts them (`warning: the
reader of our stdout was not keeping up: N line(s) of the agent's output
were not echoed`). If that reader is still stuck when the run ends, the
leash gives up on it after two seconds without progress, so the summary and
the exit code are not held hostage. Non-JSON lines on that
stdout (warnings, entrypoint chatter) are not telemetry and are ignored; a
telemetry event whose fields are malformed (`"input_tokens": "abc"`) trips
the run — the guard fails closed rather than guess.

`--receipt path.json` creates a missing directory, and a path that cannot
be written to at all is refused *before* the agent starts (exit `2`) — not
discovered after the money was spent. Should the write still fail at the
end (disk full), the receipt summary is printed on stderr instead, with a
warning, even under `--quiet`.

### Stopping the leash (Ctrl-C, SIGTERM, SIGHUP, SIGQUIT)

The agent runs in its own process group, so a Ctrl-C in your terminal (or a
`kill` from a supervisor) reaches **only the leash**. The leash then kills
the agent's whole process tree, writes the receipt (`STOPPED: interrupted
(Ctrl-C)` / `terminated (SIGTERM)`) and exits `130` / `143` — it never dies
with a traceback leaving the agent running. Closing the terminal or losing
an SSH connection is a SIGHUP, and Ctrl-\ a SIGQUIT: both are handled the
same way (`terminated (SIGHUP)`, exit `129`; `terminated (SIGQUIT)`, exit
`131`). Before 0.1.1 either one killed the leash on the spot and left the
agent running, uncapped, with no receipt. A hangup the parent chose to
ignore stays ignored, so `nohup shortleash run …` still does what nohup is
for: the run carries on without its terminal. A second Ctrl-C during the
grace period force-kills at once. This holds at every point of the run,
including the window after the agent itself has exited while the leash
waits for stray processes it left behind (see *Killing the tree*): a signal
there kills them at once, with no grace, and the receipt says `STOPPED:
interrupted (Ctrl-C): the leash was signalled after the command exited and
killed the process(es) it left running`. On Windows, Ctrl-Break is handled
like SIGTERM.

SIGKILL (and a crash) cannot be handled: no receipt is written. On Linux the
kernel is asked to SIGKILL the agent together with the leash
(`PR_SET_PDEATHSIG`, best effort — the agent itself, not the processes it
spawned); on macOS nothing does, so a `kill -9` of the leash leaves the
agent running; on Windows the kill-on-close Job Object ends the tree.

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
- A claim is advisory, so a fleet database that cannot be used (a corrupt
  `fleet.db`, a home that cannot be written) is a warning, not a stop:
  `run --fleet` prints `fleet WARNING: … running WITHOUT a claim`, records
  it in the receipt and runs the agent; `fleet list` prints one
  `shortleash: error:` line and exits `1`. Before 0.1.1 both were a
  traceback, and the run never started.

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
| `124` | a cap tripped and the tree was killed (mirrors `timeout(1)`) — or a cap turned out to be exceeded only in the agent's *final report*, after the run had ended (`EXCEEDED:` in the receipt, `breach_live: false`; see *Live, or only afterwards*) |
| `125` | a budget/token/spawn/anomaly cap was requested but there was nothing to enforce it against: the child produced **no usage telemetry**, or — for the spawn cap — no per-message events. The cap could not have been enforced (see *Trust boundary*) |
| `130` / `143` / `129` / `131` | the leash itself got Ctrl-C / SIGTERM / SIGHUP / SIGQUIT (Ctrl-Break on Windows is `143`) — at any point, the wait for stray children after the agent exited included; tree killed, receipt written |
| `1` | the process exited 0 but its final stream-json `result` said `is_error: true` (`FAILED:` in the receipt, e.g. "Not logged in") |
| `128+N` | POSIX: the child was killed by signal N outside any breach (what a shell would report) |
| `126` | the command was found but could not be started (a directory, no execute permission, not an executable file, a path through a file): one `shortleash: error: cannot start …` line and no receipt, as nothing ran. Before 0.1.1 this was a traceback and exit `1` |
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
completes, and the agent's final report is compared with every cap once the
run is over. The receipt JSON also carries `telemetry_seen`, `usage_events`,
`enforced` / `not_enforced` (which caps actually held), `price_assumed`
(the estimate had to assume a rate for an unknown model), `enforced_live` (for
each enforced cap: `true` if it could stop the run while it ran, `false` if
it was only checked against the final report afterwards), `breach_live`
(`true`: the leash ended the run; `false`: the breach was only found after
the run was over; `null`: no breach), `anomaly` (whether
anomaly mode was armed, and on how many runs), `pricing` (`as_of`,
`source`), `agent_error` (the `is_error` verdict), `child_returncode` (what
the child itself returned, untouched), `hint` and `warnings` (stray child
processes, a closed stdout, corrupt history lines, an un-armed anomaly cap,
a receipt that could not be written, a history that could not be written).
The JSON never contains a bare `Infinity` or `NaN`: a figure that is not a
finite number (an agent reporting a usage count beyond any float) is
`null`, the receipt says `cost: n/a (not a finite number)` with a warning,
and such a run is not added to the history.

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
`[input, output]` dollars per 1M tokens. The entries of your file are tried
first and the built-in table after them; within each, the **longest**
matching key wins, so `{"opus-4-5": [5, 25]}` prices `claude-opus-4-5` at
5 / 25 while other opus models keep the built-in rate (before 0.1.1 the
built-in `opus` entry won and the override was silently ignored). Rates
must be finite numbers >= 0 and a key must not be empty: `NaN`, `Infinity`,
negative values and `""` are refused with exit `2` (a NaN rate would have
let every run through).

A model that matches **no** entry is priced with the `default` entry
(Sonnet's rate) — and the leash says so instead of guessing quietly: one
`[shortleash] warning: model 'x' is not in the pricing table …` line on
stderr as soon as the model is known (not under `--quiet`), the same text
as a receipt `warning:`, and `"price_assumed": true` in the receipt JSON.
Add the model to your pricing file to silence it.

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

### Live, or only afterwards

A cap can stop a run only while the agent is still telling you what it
spends. What the leash can do depends on what the agent prints:

| the agent prints | checked | when |
|---|---|---|
| a stream-json event per message (`claude -p --output-format stream-json --verbose`, added automatically when you wrap `claude`) | budget, tokens, spawns, anomaly after every `assistant` event, and again against the final `result` | **live** — the tree is killed the moment a cap is crossed |
| only a final `result` (a single JSON object at the end) | budget, tokens and (when armed) anomaly, from the `result`'s `total_cost_usd` / `usage` | **after the run ended** — too late to stop anything, but reported honestly |
| anything else | nothing but `--timeout` | see *No telemetry, no comfort* |

Whether `claude -p --output-format json` prints exactly that one `result`
object has **not been verified against the real `claude` CLI**; the leash
treats any output of that shape the way the second row says.

When the agent's final report shows a cap exceeded after the fact, the run
is *not* "within guardrails": the leash exits `124`, the receipt says
`EXCEEDED:` (not `STOPPED:` — nothing was stopped), the reason says
`after the fact`, and `breach_live` is `false`:

```
  cost:      $50.0000 (reported)
  EXCEEDED:  budget exceeded after the fact: $50.0000 reported by the agent > $1.00 cap (no live usage events were seen, so nothing could be stopped while it ran)
```

A run that stayed within the caps but was checked only afterwards still
ends `finished within guardrails ✓`, with a `warning:` line saying the cap
was checked only after the run (`enforced_live` is `false` for it). The
spawn cap needs the per-message events (only they list the `tool_use`
blocks): with a final `result` alone it is reported under `not_enforced`
and the exit code is `125`. A final report whose cost is not a finite number
(`1e999`, `NaN`) is malformed telemetry and trips the run, like any other
malformed field.

## How it works

- Child runs in its own process group on POSIX (`start_new_session=True`)
  and in a kill-on-close Job Object on Windows (`kernel32` via ctypes,
  `taskkill /T /F` as the fallback; see `proctree.py`) — a kill takes the
  whole tree, never your shell.
- A monitor thread parses each stream-json line (`system/init`,
  `assistant` usage + subagent `tool_use` spawns, `result` cost/`is_error`)
  and checks caps after every observation; a second thread echoes the
  lines to your stdout, so nothing about your terminal can delay a check.
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
- **Without per-message events a cap cannot stop anything.** An agent that
  reports its spend only in a final `result` is checked after it has
  finished (`EXCEEDED:`, `enforced_live: false`), not while it runs.
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

## Scope / limits (v0.1.1)

- Claude Code `-p` (non-interactive) is the supported path. Interactive
  sessions and other agents (Codex, Cursor) are on the roadmap — the
  event parser is isolated in `events.py` for exactly this.
- Cost mid-run is an estimate; treat `--budget` as a guardrail, not accounting.
- **Live enforcement needs per-message events.** An agent that reports only
  a final `result` can be checked after it has finished, not stopped (see
  *Live, or only afterwards*). Whether the real `claude` CLI's
  `--output-format json` has that shape has **not been verified** here.
- **The agent's own report is the evidence.** The leash cannot see spend an
  agent hides or under-reports (see *Trust boundary*); a final report is
  compared with the caps but not audited.
- **SIGKILL of the leash cannot be handled** (no receipt). On Linux the
  agent is asked to die with it, but only the agent itself, not what it
  spawned; macOS has no equivalent; Windows relies on the Job Object.
- **A reader of our stdout that stops reading** costs the echo, not the
  caps: lines are dropped (and counted) once 4 MB are queued, and the leash
  stops waiting for that reader two seconds after its last progress. Until
  then the leash's own exit can wait for it.
- **Windows:** SIGHUP and SIGQUIT do not exist there; Ctrl-Break and the
  kill-on-close Job Object are what apply. The signal, echo-thread and
  exit-code changes of 0.1.1 were written to be platform-neutral and are
  covered by tests that run on Windows too, but they were developed and run
  on Linux only.
