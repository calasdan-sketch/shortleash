# ShortLeash — stop runaway agents

**A seatbelt, not a sandbox.** A circuit breaker for AI coding-agent runs — and a spawn-storm guard. Wraps an agent command, tails its
telemetry in real time, and **kills the run the moment it breaches a guardrail**
— budget, subagent-spawn cap, token cap, or wall-clock timeout. Emits a
per-run receipt showing what it cost and why it stopped. "The moment" needs
an agent that streams its usage; an agent that only reports at the end can be
checked, but no longer stopped (see *Live, or only afterwards*).

**Limits:** It is a seatbelt, not a sandbox: it stops a run that crosses a limit, but it cannot stop an agent from doing harm inside the limits, and `kill -9` of the leash itself leaves the agent running on macOS.

Vendors' "budgets" only warn while the meter keeps running. This one halts.

## Install

Zero dependencies — Python 3.10+ stdlib only.

```bash
pip install shortleash
shortleash run --budget 2.00 -- <command>

# or from source
pip install git+https://github.com/calasdan-sketch/shortleash

# or, from a checkout, without installing:
python3 -m shortleash run --budget 2.00 -- <command>
```

Written for Linux, macOS and Windows (Python 3.10+); the CI matrix runs the suite on those three systems. On Windows, `claude`
is found through `PATHEXT` (the npm `claude.cmd` shim — see *Windows*
below), and console output never dies on a cp1252 code page — unencodable
characters are replaced.

## Spawn-storm guard (`--max-spawns`)

The failure that burns real money fast is an agent that launches subagents
that launch more subagents. `--max-spawns N` counts every subagent launch in
the stream and stops the run when the count goes past N:

```
$ shortleash run --max-spawns 3 -- claude -p "audit every package"
  STOPPED:   spawn cap exceeded: 4 subagents > 3 max
  culprit:   4 spawns, 87% of tokens in agent Explore
  hint:      stopped by the spawn cap. The leash stops the run as soon as it
             reads the launch that went over the limit, but subagents that had
             already started may have used tokens by then. ...
```

(That output is from a made-up agent that prints a stream of launches, run
with `python3 storm.py 8` in the 0.2.0 tests; it was not a real `claude` run.)

It **stops quickly, not instantly**: the leash reads the launch, then kills the
process tree; subagents already running may spend tokens in between, and an
agent that reports only at the end cannot be stopped live at all (see *Live,
or only afterwards*). Treat it as a brake, not a guarantee.

**The culprit line.** When a cap trips, the receipt (and the JSON receipt's
`culprit` and `agents` fields) say which kind of agent used the tokens that
grew since the last event, for example `38 spawns, 71% of tokens in agent
Explore`, or `most tokens (62%) were used in the main conversation` when the
subagents were not the problem. Tokens are attributed by the event that
reported them (`parent_tool_use_id`); an event that names no agent counts as
the main conversation.

**Early warning (forecast).** With a `--budget` or `--max-tokens` cap, a line
like `forecast: $0.55 of the $2.00 budget used after 1m 10s (27%); at this
pace the budget is used up in about 3m 5s` is printed the first time the run
passes 20%, then 50% and 80%. It is the pace so far carried forward, nothing
smarter, so a bursty run will not match it. It stays quiet during the first
five seconds and when the cap is more than an hour away. It never stops
anything. `--no-forecast` turns it off.

## Per-repo limits (`.shortleash.toml`)

Put the limits next to the code so everyone who runs the agent in that repo
gets them:

```toml
# .shortleash.toml
budget = 2.50        # USD
max_spawns = 10
max_tokens = 800000
timeout = 900        # seconds
forecast = true
profile = "docs"     # baseline name for --anomaly

[hook]               # used only by `shortleash hook`
max_spawns = 12
```

`shortleash run` looks in the current folder and then in each folder above
it, uses the first file it finds and says so (`limits from <path>`). A flag on
the command line always beats the file. `--config FILE` names a file;
`--no-config` ignores it. Unknown keys, wrong types and negative numbers are
an error (exit 2) rather than a limit that silently does nothing. The file
cannot run anything. Python 3.11+ reads it with `tomllib`; on 3.10 a small
built-in reader handles exactly the syntax above (numbers, strings, `true` /
`false`, one level of `[table]`, `#` comments) and refuses anything else.

## Hook mode (`shortleash hook`) — a spawn limit inside a session

`shortleash run` wraps one command. If you work in an interactive Claude Code
session, install the hook instead and Claude Code asks shortleash before each
subagent:

```bash
shortleash hook install --max-spawns 8      # ~/.claude/settings.json
shortleash hook install --scope project     # ./.claude/settings.json
shortleash hook install --dry-run           # print, change nothing
shortleash hook status                      # what recent sessions started
shortleash hook uninstall                   # removes exactly what install added
```

Two entries are merged into the `hooks` of your settings file (the schema
was checked against https://code.claude.com/docs/en/hooks in October 2026 and
lives in one function, `hook_entries()`):

- `PreToolUse`, matcher `Agent|Task`: counts the session's subagent starts
  and, past the limit, answers `permissionDecision: "deny"` with a reason
  Claude reads. This is what stops a spawn.
- `SubagentStart`: records the agent type for `hook status`. Per the docs it
  cannot block anything.

Install never overwrites or reorders your own hooks, keeps a `.bak` copy,
keeps your indentation and line endings, refuses a settings file that is not
valid JSON, and running it twice changes nothing. The command written is the
absolute path of the `shortleash` console script; if that cannot be found it
falls back to `python -I -m shortleash` with the absolute interpreter, and
tells you, because that form only works while this Python can still import
shortleash. The cap comes from `$SHORTLEASH_MAX_SPAWNS`, then `.shortleash.toml`
(`[hook]` first), then what `install --max-spawns` saved.

What hook mode **cannot** do:

- It cannot stop a subagent that is already running or give back tokens
  already spent; it refuses the next start.
- It counts spawns, not money. Budget and token caps need `shortleash run`.
- It counts per `session_id`; a new session starts at zero.
- A resumed subagent or an agent-team message is not an `Agent` tool call and
  is not counted.
- It **fails open**: if the command moved, Python is gone or the state folder
  is not writable, Claude Code carries on and nothing is limited. `hook
  status` shows whether it has been counting.
- Anyone who can edit your Claude Code settings can remove it.
- It has been checked against the documentation and with simulated hook
  input, never run by a real Claude Code (see the limits below).

## `shortleash init` and `shortleash report`

`shortleash init` is the one-command start: it installs the hook (the same
idempotent install as `hook install`, with a limit of 10 subagent starts per
session unless you already set one) and writes a starter `.shortleash.toml` in
the current folder: `budget = 5.00`, `max_spawns = 10`, `timeout = 1800`
(30 minutes). A profile that already exists is never overwritten. `--dry-run`
changes nothing; `--scope`/`--settings` pick the settings file.

The early-warning lines at 20%, 50% and 80% of a budget or token cap
(`forecast`, on by default) are the soft warning: they go to stderr and never
stop anything.

`shortleash report [--days 7] [--json]` sums the history file
(`~/.shortleash/history.jsonl`): runs, cost and tokens per agent/profile and
per UTC day. It counts only what the history holds (runs with telemetry); the
costs are the receipts' estimates or the agent-reported totals, not an invoice.

State files (`~/.shortleash`, the hook's session files) are created readable by
you only (0700 folders, 0600 files, POSIX); hook session files older than 7
days are removed when a new session starts. When the built-in price table is
more than 90 days old (or a `--pricing` file has no usable `as_of`) `run`
prints a warning on stderr.

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
automatically (the guard needs the telemetry stream), unless you pass
`--output-format` yourself, as `--output-format json` or
`--output-format=json`: your choice is kept, and what the leash can then do
depends on what the agent prints (see *Live, or only afterwards*). `claude` is
recognised by its file name in any letter case, with or without a Windows
executable suffix (`.exe`, `.cmd`, `.bat`, `.com`, `.ps1`). The file systems
of Windows and macOS are normally case-insensitive, so `Claude` and
`CLAUDE.EXE` start the very program `claude` does; before 0.1.3 a capital
letter in the name itself (`Claude`) kept the flags off.

The agent's stdout is echoed to yours so you still watch it work, but the
echo is a best-effort copy, not the original bytes. The leash reads the
output as UTF-8 text, a line at a time: a byte that is not valid UTF-8 comes
out as U+FFFD (the replacement character), a carriage return or CR LF ends a
line and comes out as a plain `\n` (a progress bar that redraws with `\r`
becomes many lines), and a console whose code page lacks a character shows
`?` in its place. On Windows the echo is written in text mode, so each line
ends in CR LF instead. Claude Code's JSON lines are unaffected by the first
two (a JSON parser reads the extra CR as white space).
The caps are checked on what the leash parsed, never on the echo.

If whatever is reading our stdout goes away (`shortleash run … | head -1`),
the echo stops but the caps keep being checked; the receipt notes it. A
reader that merely *stalls* (a paused pager, a pipe nobody drains) cannot
blind the caps either: every line is parsed first and handed to the echo
through a bounded queue (4 MB) on a thread of its own. When the queue is
full — a stalled reader, or output arriving faster than a reader can take it
— lines are dropped, never waited for, and the receipt counts them (`warning:
the reader of our stdout was not keeping up: N line(s) of the agent's output
were not echoed`). How much of a fast burst still gets through depends on the
reader and the machine; no amount is promised. If that reader is still stuck
when the run ends, the leash gives up on it after two seconds without
progress, so the summary and the exit code are not held hostage.

Two limits you should know about. The unit that is queued, counted and
dropped is a *piece* of up to 64 KB, not always a whole line: when a very
long output line meets a full queue, the reader gets that line with a gap in
it, and the warning's "line(s)" really counts pieces. And an output line
longer than 8 million characters (8 MiB of text) is never parsed for
telemetry at all: it is skipped, the receipt says so
(`an agent output line longer than 8388608 characters was skipped`), and any
usage inside it is not counted, so a cap cannot see it. Normal stream-json
lines are far below that.

Non-JSON lines on the agent's stdout (warnings, entrypoint chatter) are not
telemetry and are ignored; a telemetry event whose fields are malformed
(`"input_tokens": "abc"`) trips the run — the guard fails closed rather than
guess.

`--receipt path.json` creates a missing directory, and a path that cannot
be written to at all is refused *before* the agent starts (exit `2`) — not
discovered after the money was spent. Should the write still fail at the
end (disk full), the receipt summary is printed on stderr instead, with a
warning, even under `--quiet`.

`--quiet` keeps the leash's own commentary off stderr: the startup banner, the
receipt text and the one-line notices (a model whose price is a guess, the id
of a fleet claim). The agent's output is still echoed, the JSON that
`--receipt` writes is still written, and errors and fleet warnings are still
printed.

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
Runs are grouped by the agent's name, the command's file name in lower case
without `.exe`, `.cmd`, `.bat`, `.com` or `.ps1` (`Claude.EXE` and `claude`
share a baseline), or by `--profile NAME` when you give one.
`--history-path FILE` keeps the history in FILE instead: runs are recorded
there, and the baseline is read from there.

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
`--profile big-refactor`. Tune with `--anomaly 5.0` and `--min-history 10`
(`--min-history` is at least `1`: `0` is refused with exit `2`, where before
0.1.2 it was a traceback). `--no-anomaly` is still accepted (it is the
default). Until the history is
long enough, anomaly mode is **not armed** and enforces nothing: the
receipt says so (`anomaly:   not armed (2 of 5 runs) — nothing was enforced
by it`, a `warning:` line, and `"anomaly": {"armed": false, "runs": 2,
"min_runs": 5, …}` in the JSON) and never lists `anomaly` under
`enforced`.

A history line that cannot be used (not valid JSON, not valid UTF-8, a figure
that is not a usable number) is skipped and is never a reason not to start the
agent: the receipt carries a `warning:` saying `history: skipped N corrupt
line(s) in FILE`. Before 0.1.3 a single byte that was not UTF-8, left by a
torn write or a hand edit, ended `run --anomaly` in a traceback before the
agent started.

### Fleet mode (`--fleet`)

Running several agents on the same machine? `--fleet` holds a **fleet
claim** on paths for the run — a lockfile for agents, merged from the
retired claimlock:

```bash
shortleash run --fleet --fleet-owner dan --budget 3 -- \
  claude -p "refactor the api layer"
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
  TTL backstop). Tune with `--fleet-owner dan` and `--fleet-ttl 3600`.
- `--fleet-owner` is the label `fleet list` shows after `held by`; without
  it the claim reads `held by ?`. `--fleet-ttl` is in seconds, from 1 to 86400
  (default 1800): a value outside that range is a usage error (exit `2`,
  before anything runs, with or without `--fleet`). Before 0.1.3 a larger
  value was silently cut down to 86400.
- `shortleash fleet list` shows who holds what right now.
- A claim is advisory, so a fleet database that cannot be used (a corrupt
  `fleet.db`, a home that cannot be written) is a warning, not a stop:
  `run --fleet` prints `fleet WARNING: … running WITHOUT a claim`, records
  it in the receipt and runs the agent; `fleet list` prints one
  `shortleash: error:` line and exits `1`. Before 0.1.1 both were a
  traceback, and the run never started.
- The claims live in a SQLite database, so fleet mode needs Python's
  `sqlite3` module. A Python built without it (some pyenv and Alpine builds)
  still runs everything else, `--version` and `run` without `--fleet`
  included; before 0.1.3 even `--version` was an ImportError traceback.
  There `run --fleet` takes the same advisory line as an unusable database
  (a warning, `running WITHOUT a claim`, and the agent runs) and `fleet list`
  prints one `shortleash: error:` line and exits `2`.

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
| `125` | a budget/token/spawn/anomaly cap was requested but there was nothing to enforce it against: the child produced **no usage telemetry** (a `usage` block with none of the token fields the leash reads counts as none: see *No telemetry, no comfort*), or — for the spawn cap — no per-message events. The cap could not have been enforced (see *Trust boundary*) |
| `130` / `143` / `129` / `131` | the leash itself got Ctrl-C / SIGTERM / SIGHUP / SIGQUIT (Ctrl-Break on Windows is `143`) — at any point, the wait for stray children after the agent exited included; tree killed, receipt written |
| `1` | the process exited 0 but its final stream-json `result` said `is_error: true` (`FAILED:` in the receipt, e.g. "Not logged in") |
| `128+N` | POSIX: the child was killed by signal N outside any breach (what a shell would report) |
| `126` | the command was found but could not be started (a directory, no execute permission, not an executable file, a path through a file): one `shortleash: error: cannot start …` line and no receipt, as nothing ran. Before 0.1.1 this was a traceback and exit `1` |
| `2` | usage error, command not found, unsafe `.cmd` launch refused, receipt path not writable, a pricing file that cannot be loaded, `fleet list` on a Python without `sqlite3` |
| other | the child's own exit code (on Windows its 32-bit status, clamped to 0–255; the raw value is `child_returncode` in the receipt) |

The last row includes a command that itself exits `124`, `125`, `126` or
`143`: by exit code alone that looks like the leash's own verdict of the same
number. The receipt tells them apart. `breached` is `true` only when the
leash ended the run, found a cap exceeded afterwards or was signalled itself;
`not_enforced` lists the caps behind a `125` of the leash's; and
`child_returncode` is what the command itself returned (`0` behind such a
`125`). The leash's own `126` writes no receipt at all.

## The receipt

Every run ends with a receipt on stderr and optionally as JSON
(`--receipt path.json`):

```
── shortleash receipt ──────────────────────
  cost:      $0.1271 (estimated)
  tokens:    51,000 (in=15,000 out=4,500 cache_r=30,000 cache_w=1,500)
  turns:     0   spawns: 1
  duration:  0.16s   exit: 124
  pricing:   built-in fallback table as of 2026-10-24 — verify against provider pricing
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
`password`, `passwd`, `bearer`, `credential` or `cookie`, or has `auth`
(or `oauth`, `authorization`) as a whole word (`--api-key sk-…`,
`--token=…`, `DB_PASSWORD=…`, `--cookie …`, `--auth x`) becomes `***`;
`--author` is not touched. So does the value of `-u`/`--user` (also glued on,
`-uuser:pass`), a `name:value` argument with such a name (`password:hunter2`),
the `user:password@` part of a URL (`https://***@host/x`), the secret part of
an HTTP header given with `-H`/`--header` (`Authorization: ***`), and any
`sk-…` key (not inside a word, so `disk-usage-report` is safe), `Bearer …`
credential, `Basic …` credential (only when it looks like base64: a word like
`basic functionality` in a prompt is left alone) or GitHub / Slack / AWS token
wherever it appears in an argument. This is pattern matching: a secret in an
unusual shape is not recognised, so keep real secrets out of command lines.
The receipt file is written with mode `0600` (owner only). **The prompt is not a secret by that rule and is stored
verbatim** — `-p "my password is hunter2"` ends up in `receipt.json` as
typed. Pass `--redact-prompt` to replace the argument after `-p`,
`--print` or `--prompt` (and a `--prompt=…` value) with `<redacted>`; a
prompt given any other way (bare positional, stdin, a file) is not
recognised. The history file never stores command text.

### Prices are a hard-coded fallback — verify them

The built-in table (`pricing.py`, USD per 1M tokens) is **not fetched from
anywhere**; it was written on the date in `PRICING_AS_OF`. The startup banner
says so (`pricing: built-in fallback table as of 2026-10-24 — verify against
provider pricing`; not under `--quiet`), the receipt on stderr repeats that
line whenever the cost it shows is an estimate, and the JSON receipt always
carries it (`pricing`: `as_of`, `source`). Check it against the provider's
pricing page (the built-in numbers were read from
<https://platform.claude.com/docs/en/about-claude/pricing>) before trusting a `--budget` to the cent, and override it with
`--pricing pricing.json` or the `SHORTLEASH_PRICING` environment variable
pointing at a JSON file:

```json
{"as_of": "2026-10-24",
 "sonnet": [3.0, 15.0], "opus": [5.0, 25.0], "haiku": [1.0, 5.0]}
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

The file is read as UTF-8 (with or without a byte-order mark) or, when it
starts with a byte-order mark, as UTF-16 or UTF-32: the same rule as the
prompt files of `cache`, so what PowerShell 5's `>` or Notepad writes works.
A file in any other encoding is one `error:` line and exit `2`. (Before 0.1.3
only plain UTF-8 was read, and a mark or UTF-16 was refused.)

A model that matches **no** entry is priced with the `default` entry
(Sonnet's rate) — and the leash says so instead of guessing quietly: one
`[shortleash] warning: model 'x' is not in the pricing table …` line on
stderr as soon as the model is known (not under `--quiet`), the same text
as a receipt `warning:`, and `"price_assumed": true` in the receipt JSON.
Add the model to your pricing file to silence it.

The estimate prices two things roughly, both on the cautious or the
cheap side as noted. A model the table does not name more precisely than its
family is priced at the family rate: `claude-3-haiku` ($0.25 / $1.25 per 1M
tokens) matches `haiku` and is priced as Haiku 4.5 ($1 / $5), so its estimate
runs about four times high (add `{"3-haiku": [0.25, 1.25]}` to your pricing
file to fix that). Cache reads are priced at 0.10x and cache writes at 1.25x
the input rate, the 5-minute rate; a cache write kept for **1 hour** costs
2x, and the leash does not model it (the stream does not say which kind a
write was in the fields the leash reads), so a run that writes a lot of
1-hour cache can cost up to 60% more on those tokens than the live estimate
says. The agent's own `total_cost_usd` replaces the estimate in the final
receipt when it is there.

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

The leash reads four fields of a `usage` block as token counts:
`input_tokens`, `output_tokens`, `cache_creation_input_tokens` and
`cache_read_input_tokens`. A block that has fields but none of those fields
(another agent's `prompt_tokens`, say) counts as no usage, not as zero
tokens: the receipt carries a `warning:` naming the fields it could not read,
a cap that needed token counts is listed under `not_enforced`, and the exit
code is `125`. A reported `total_cost_usd` still backs `--budget`. Before
0.1.2 such a block counted as zero tokens, so `--max-tokens 100` and a report
of 5,000,000 tokens in other words ended `finished within guardrails`.

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
- **Prices are a dated fallback table**, not live data: the startup banner
  and the JSON receipt carry its `as_of` date — verify against provider
  pricing.

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

The prompt file is read as UTF-8 (with or without a byte-order mark) or, when
it starts with a byte-order mark, as UTF-16 or UTF-32 (PowerShell 5's `>`
writes UTF-16; the `--pricing` file is read the same way); the prompt `-o`
writes is plain UTF-8. A file in any other encoding, a file that cannot be
read and an output path that cannot be written are each one `error:` line and
exit `2`. (Before 0.1.2 the first and the last were a traceback and exit `1`.)

## Testing

```bash
python3 -m unittest discover -s tests -v   # or: python3 -m pytest -q
# `-s tests -t .` works too
```

The source distribution carries `tests/` and `sim/` as well, so the same
command works in an unpacked archive. (Before 0.1.3 it lacked
`tests/_isolate.py` and `sim/runaway.py`, so most of the test files failed
to import.)

`sim/runaway.py` is a fake agent emitting realistic stream-json — the suite
kills it for real (budget, spawn, token, and timeout breaches) without
spending a cent. It also spawns escaped grandchildren to prove the leash
returns and kills the whole tree, sends the leash itself Ctrl-C and SIGTERM
(POSIX), closes its stdout mid-run, and never touches your real
`~/.shortleash` (state lives under `SHORTLEASH_HOME`, which the tests point
at a temp dir). The Windows shim resolution and the cmd.exe refusal list
are unit-tested with a fake shim directory on every platform.

## Scope / limits (v0.2.0)

- Claude Code `-p` (non-interactive) is the supported path. Interactive
  sessions and other agents (Codex, Cursor) are on the roadmap — the
  event parser is isolated in `events.py` for exactly this.
- Cost mid-run is an estimate; treat `--budget` as a guardrail, not accounting.
- **The model is read from the stream's `system`/`init` event only.** Every
  live estimate is priced at that model's rate, even if a later `assistant`
  event names another model (a subagent on a dearer one, say), so a cost cap
  can trip later (or earlier) than the real spend warrants; the agent's own
  `total_cost_usd` replaces the estimate when the run completes and is
  compared with the caps then. Whether a real Claude Code stream ever names a
  different model that way has not been verified here: the behaviour was
  only seen on a stream made up for the test.
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
- **A reader of our stdout that stops reading, or cannot keep up with a
  burst,** costs the echo, not the caps: lines are dropped (and counted)
  once 4 MB are queued, and the leash stops waiting for that reader two
  seconds after its last progress. Until then the leash's own exit can wait
  for it. The echo is a best-effort copy of the output (see *Usage*).
- **Windows:** SIGHUP and SIGQUIT do not exist there; Ctrl-Break and the
  kill-on-close Job Object are what apply. The signal, echo-thread and
  exit-code changes of 0.1.1, and the changes of 0.1.2 (UTF-16 prompt files
  for `cache`, `--output-format=`, `--min-history`, usage blocks the leash
  cannot read, the echo wording), were written to be platform-neutral and are
  covered by tests that run on Windows too (the suite passed there on Python
  3.11 for 0.1.2), but they were developed on Linux: no file written by a real
  PowerShell 5 was read.
- **Hook mode and the spawn counter were never run by a real Claude Code,
  and the culprit/forecast text was only seen on made-up streams.**
- **What 0.2.0 was run on:** Linux only, Python 3.10, 3.13 and 3.14 and the
  unpacked source archive. Not on Windows or macOS, and not against a real `claude`. Agent names
  in other letter cases are tested by name (and with a script really called
  `Claude` on POSIX), a Python without `sqlite3` is simulated, and the
  byte-order-mark and UTF-16 files are built byte by byte, not written by a
  real PowerShell.
