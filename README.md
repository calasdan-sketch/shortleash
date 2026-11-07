# ShortLeash — stop runaway agents

**A circuit breaker for Claude Code: it kills a run that overspends, loops or
stalls, caps what ALL your agents spend in a day, asks a human before spending
past a threshold, and leaves a receipt for every run that an auditor can
check.** Zero dependencies, Python 3.10+, Linux / macOS / Windows.

A circuit breaker for AI coding-agent runs. Wraps an agent command, tails its
telemetry in real time, and **kills the run the moment it breaches a guardrail**
— budget, subagent-spawn cap, token cap, loop cap, idle (stuck) timeout or
wall-clock timeout. Emits a per-run receipt showing what it cost and why it
stopped. "The moment" needs an agent that streams its usage; an agent that
only reports at the end can be checked, but no longer stopped (see *Live, or
only afterwards*). For **interactive** Claude Code sessions the same caps,
the loop guard and an approval gate run as a hook (see *Interactive sessions*).

```console
$ shortleash run --budget 2 --ask-over 1 --max-repeats 4 --idle-timeout 300 -- \
    claude -p "fix the flaky test"
[shortleash] guarding: claude
[shortleash] budget=$2 max_repeats=4 idle_timeout=300s ask_over=$1
[shortleash] 50% of budget used: $1.0012 of $2 after 4m10s ($0.2402/min); at this rate the cap is reached in ~4m09s
[shortleash] APPROVAL NEEDED: estimated $1.0012 > $1.00 approval threshold (--ask-over), after 4m10s. The agent is paused.
[shortleash] Let it continue up to the $2.00 budget? [y/N] (no answer in 5m00s = stop) n
...
  STOPPED:   approval refused: estimated $1.0012 > $1.00 approval threshold (--ask-over); a human refused to let it continue
```

(An illustration of the output format; the figures are made up. Try it for
free with the fake agent: `python3 -m shortleash run --budget 2 --max-repeats
4 -- python3 sim/runaway.py` stops on a loop of identical `Task` calls.)

## How it compares

Only what the linked pages say (or what was run, where marked), checked
2026-11-07; "no" means the page does not offer it.

| | ShortLeash | Claude Code `--max-budget-usd` | Anthropic spend limits | LiteLLM proxy budgets | AgentBudget | ccusage |
|---|---|---|---|---|---|---|
| stops a run that overspends | yes, kills the process tree | yes, `-p` only (`claude --help` 2.1.319: "only works with --print"; runs overshot a $0.03 cap by ~7% in [one test](https://mer.vin/news/what-max-budget-usd-actually-kills-mid-agent-loop/)) | yes, per month: requests then fail with HTTP 429 / 400 ([docs](https://platform.claude.com/docs/en/api/rate-limits)) | yes, rejects requests ([docs](https://docs.litellm.ai/docs/proxy/users)) | yes, raises before the next call ([PyPI](https://pypi.org/project/agentbudget/)) | no: reports only (ran v20.0.48: `daily`, `session`, `blocks` print usage; `blocks --token-limit` only warns) |
| cap over ALL agents for a day / a project | yes: `--daily-budget`, `--project-daily-budget`, `run` and `hook` share one ledger | no | month only, per organization or workspace | yes, `budget_duration` windows such as `24h` per key / team (needs Postgres) | no | no |
| interactive Claude Code sessions | yes, as a hook | no (`-p` only) | yes (account level) | only if Claude Code is pointed at the proxy | no | no (`statusline` shows cost) |
| a session with no tool calls can be stopped | `--block-prompts` refuses the next prompt | n/a | n/a | n/a | n/a | n/a |
| needs a server / code changes | no | no | no | yes: proxy + Postgres | yes: SDK in your code | no |
| loop detection | yes, stops: exact, fuzzy (`--loop-fuzzy`) and error streaks (`--max-errors`) | no | no | `max_iterations` per agent session ([docs](https://docs.litellm.ai/docs/proxy/users)) | yes (`max_repeated_calls`) | no |
| stuck (no output) timeout | yes | no | no | no | no | no |
| human approval past a threshold | yes: pauses the agent and asks, or Claude Code's prompt in hook mode | no | no | no | soft-limit callback only | no |
| kills what the agent detached (setsid, daemons) and survives `kill -9` of the supervisor | yes on Linux (sentinel + ancestry sweep, below) | n/a | n/a | n/a | n/a | n/a |
| tamper-evident receipts | yes: hash chain, optional HMAC, `shortleash verify` | the final `result` event | n/a | spend logs in the DB | a report object | no |

Not in the table because they are not the same kind of tool:
[claude-code-router](https://github.com/musistudio/claude-code-router) routes
requests and offers per-client request / token / image limits, and its page
mentions no dollar budget or loop detection;
[Portkey](https://portkey.ai/docs/product/ai-gateway/virtual-keys/budget-limits)
has USD and token budgets with weekly / monthly resets, on its Enterprise plan
and for select Pro customers; Langfuse and AgentOps track and alert.
Helicone was not checked.

## Why

A coding agent that loops (re-running the same failing command), stalls, or
fans out into subagents keeps spending until someone notices. Observability
tools tell you afterwards; provider budgets are per account, not per task.
ShortLeash is one command around one task: caps it, watches it, stops it, and
writes down what happened.

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
  --max-repeats 4 --idle-timeout 600 --timeout 3600 \
  --ask-over 3.00 --receipt ./receipt.json -- \
  claude -p "migrate the database layer"

# In CI: a receipt in the job summary
shortleash run --budget 2 --receipt-md "$GITHUB_STEP_SUMMARY" -- claude -p "..."
```

### Loops and stalls (`--max-repeats`, `--idle-timeout`)

`--max-repeats N` stops a run whose agent makes **the same tool call N times
in a row** — same tool, same input, and, when the result is known, the same
result — or repeats the same cycle of two to four calls N times (`Read a.py,
Bash pytest, Read a.py, Bash pytest, …`). Calls with different inputs never
form a loop, so a healthy edit-then-test cycle is not flagged however long it
runs: each edit is different; and re-running the same test is not a loop
while its output keeps changing (`5 failed`, `4 failed`, …). Trailing white
space in inputs and results is ignored. Polling tools made to be called
again and again (`BashOutput`, `TaskOutput`, `TaskGet`, `TaskList`,
`Monitor`, …; the list is `loops.POLLING_TOOLS`) never form a loop on their
own. `N` is at least 2; 3 to 5 is a sensible start. The reason names the tool and a short
hash of its input (`loop detected: the same Bash call (input 3fa2c1d0) was
made 4 times in a row`), never the input itself, which may hold a secret.
Like the spawn cap, it needs per-message events (`max_repeats` is listed
under `not_enforced` and the exit code is `125` without them).

By default matching is exact, so the same call with a trivially different
input (another timestamp, a shifted line range, a space in the middle) or a
result that changes every time (a clock, a PID) is not a repeat. Two opt-in
settings close most of that gap, and say what they cost:

- `--loop-fuzzy` compares calls with white space runs and every number in
  the input ignored, and results with timestamps, UUIDs, long hex ids,
  durations (`3.2s`) and `pid N` ignored. Other digits in a result still
  count, so "5 failed" then "4 failed" is progress, not a repeat. The cost:
  a sweep such as `check.py --seed 1`, `--seed 2`, ... that prints the same
  "ok" each time looks like a loop (the first false positive in
  *Benchmarks*). Raise `N` or leave fuzzy off for such work.
- `--max-errors N` stops the run after N tool calls **in a row** failed
  (`is_error` on the tool result): an agent that varies its commands is not
  repeating itself, but eight different `pip install` attempts that all fail
  are not getting anywhere either. One successful call resets the count; in
  hook mode each subagent has its own count. A tool call you refuse in
  Claude Code's prompt is also an error result, so refusing six in a row trips
  it. Like `--max-repeats` it needs per-message events.

Even then it catches literal retries, near-literal ones and failure streaks,
not every way an agent can go round in circles.

`--idle-timeout SECONDS` stops a run that has printed nothing for that long
(`stuck: no output from the agent for 600s`). It does not depend on the
agent's telemetry, like `--timeout`. Claude Code prints an event when a
message completes, not while a tool runs: set it above your longest single
tool run (a full build, a test suite), or it will stop a healthy run.

### Approval gate (`--ask-over USD`)

Past `--ask-over` (an estimated spend below `--budget`), the leash **pauses
the agent's whole process tree** (SIGSTOP to its process group), asks on
your terminal whether it may continue up to `--budget`, and waits
`--ask-timeout` seconds (default 300). `y` resumes it; anything else, no
answer in time, no terminal to ask on (CI, a service, a background job) or a
platform that cannot pause the tree (Windows) **stops the run**: the gate
fails closed. It asks once per run. The receipt carries `approval`:
`{"threshold_usd", "cost_at_ask_usd", "answer", "waited_s"}`. The wall-clock
`--timeout` keeps counting while it waits (and still kills a paused agent);
`--idle-timeout` does not. `--ask-over` must be below `--budget` (exit `2`
otherwise).

### Spend forecast (`--warn-at`)

With `--budget`, the leash prints one line when the live estimate crosses
50% and 80% of it: what is spent, the rate, and when the cap will be
reached at that rate. `--warn-at 0.25,0.5,0.9` picks other fractions,
`--warn-at off` none; `--quiet` silences it. The receipt carries
`burn_rate_usd_per_min` and `budget_used_pct` — the numbers to size the
next run's caps by.

### Markdown receipt (`--receipt-md PATH`)

Appends the receipt as a Markdown table to PATH — appended, not replaced, so
`--receipt-md "$GITHUB_STEP_SUMMARY"` puts it in a GitHub Actions job
summary and several runs can share one file. The command and the prompt are
left out (a summary is shown to everyone who can see the job), and text from
the agent is escaped (no HTML, no links). A path that cannot be written is
refused before the agent starts (exit `2`).

### Day and project budgets (`--daily-budget`, `--project-daily-budget`)

`--budget` caps one run. Ten sessions at $9 each stay under a $10 cap and
still cost $90. The **spend ledger** adds caps over everything on the
machine, for the local calendar day, shared by `run` and `hook`:

```bash
shortleash run --daily-budget 25 --project-daily-budget 10 -- claude -p "..."
shortleash hook install --daily-budget 25 --project-daily-budget 10 \
  --write .claude/settings.json
```

Every run and session appends its cumulative cost to its own file under
`<state>/ledger/` (one short line per $0.005 of growth; nothing is ever
rewritten, so concurrent agents cannot lose each other's writes and no lock
is needed). Today's spend is the sum over those files; a session that crosses
midnight is split between the days. `--project-daily-budget` counts only the
same project: for `run`, the working directory; for `hook`, the directory
Claude Code keeps the session's transcripts in (named after the project's
path with every character that is not a letter or digit turned into `-`;
that this matches for every path was not verified).

Limits: the first line of a session carries everything it had spent before
it was first seen, so a long session adopted by the hook today counts in full
today (an over-count, the safe side); two agents can each pass the check just
before the other's spend lands, so the cap can be overshot by about what the
agents spend between two of their own checks (one tool call for the hook, a
second for `run`); the cost is the same estimate as everywhere else; and a
ledger that cannot be written is a usage error (`run` exits `2` before
starting, the hook exits `2` with `--fail-closed` and `1` otherwise) rather
than a cap that quietly does not hold. It is a guardrail against a runaway,
not a defence against an agent that deletes the ledger. With only a final
`result` and no per-message events the caps are `not_enforced` (exit `125`).
The receipt carries `ledger` (`day_spend_usd`, `project_spend_usd` and the
caps).

### Receipts an auditor can verify (`--chain`, `shortleash verify`)

```bash
shortleash run --chain ~/receipts.jsonl --budget 2 -- claude -p "..."
shortleash verify ~/receipts.jsonl
# OK: 14 record(s), head 7837f7da... -- write the head down
shortleash verify ~/receipts.jsonl --expect-head 7837f7da...
```

`--chain PATH` appends the (secret-scrubbed) receipt to a JSON-lines log in
which each record holds the SHA-256 of the record before it. `verify` names
the first record that was edited, removed, inserted or moved and exits `1`.
What that does and does not prove:

- Dropping the **last** records leaves a valid, shorter chain. Keep the head
  hash that `run` prints and `verify` reports somewhere the log's writer
  cannot reach, and pass it as `--expect-head`.
- Hashes alone do not stop someone who can write the file from rewriting the
  whole chain consistently. Put a secret in an environment variable that the
  agent cannot read (`SHORTLEASH_CHAIN_KEY`, or any name via
  `--chain-key-env` / `verify --key-env`; never on the command line) and each
  record also carries an HMAC-SHA256: a chain rewritten without the key fails
  `verify --key-env`. Whoever holds the key can forge.
- The contents are only as true as the run's telemetry (see *Trust
  boundary*); a record says what the leash saw, when the host's clock said so.
- Concurrent runs extend one chain under a lock on `PATH.lock`. A chain whose
  last line is cut short is not extended (`run` exits `2`; look at it with
  `verify`). The lock is `flock` on POSIX and `msvcrt` on Windows (the
  Windows path was not run).

## Interactive sessions (`shortleash hook`)

An interactive Claude Code session has no stream to wrap, but it runs
**hooks**: before each tool call it starts a command, gives it the event as
JSON, and lets it allow, deny or escalate the call to you. `shortleash hook`
is that command:

```bash
shortleash hook install --budget 10 --ask-over 6 --max-repeats 4 --max-spawns 8
# prints the settings block; to merge it into a settings file instead:
shortleash hook install --budget 10 --ask-over 6 --max-repeats 4 \
  --write .claude/settings.json      # the old file is kept as .bak
```

On each tool call it reads the session's transcript (the
`transcript_path` Claude Code passes, plus the subagent transcripts next to
it), counts the spend per model with the same pricing table, and:

| condition | what Claude Code is told |
|---|---|
| over `--budget` or `--max-tokens` | `deny` and `continue: false` — Claude stops; you can still talk to it, and every further tool call is denied |
| the call would be subagent number `--max-spawns` + 1 | `deny` that spawn, with the reason |
| the call repeats a loop (`--max-repeats`) | `deny` it, with the reason, so Claude can change course |
| over `--ask-over` | `ask` — **Claude Code's own permission prompt asks you** about each further call |
| otherwise | nothing: no output, the normal permission rules apply |

**Fast hook.** Claude Code starts the hook as a new process before every
tool call, and a session with many subagents can hold hundreds of MB of
transcripts. The hook therefore keeps, under `<state>/hook-cache/`, the
state after the bytes it has already read of each transcript (usage per
model, subagent spawns, the recent calls) and reads only what was appended.
A cache that is missing, corrupt, from another version, or for a file that was
truncated or replaced is dropped and the file is read again from its start; the
figures are the same as a full read (a test replays random growth and compares
the two). `SHORTLEASH_NO_CACHE=1` switches it off. The cache is as
trustworthy as the transcript: an agent that can edit files in your state
directory can make the hook count less (the same is true of the transcript
itself).

**Prompts.** A hook runs at tool calls, so a session that only talks is never
stopped by `PreToolUse`. `hook install --block-prompts` also registers the
hook on `UserPromptSubmit`: a session over `--budget`, `--max-tokens`,
`--daily-budget` or `--project-daily-budget` then has its next prompt
refused (`{"decision": "block", "reason": ...}`, per the
[hooks reference](https://code.claude.com/docs/en/hooks)). The prompt text is
never logged. `--ask-over` and the loop caps do not block prompts. Not tried
against a live Claude Code here.

Every deny and ask is a line in `hook-log.jsonl` in the state directory
(`~/.shortleash` or `$SHORTLEASH_HOME`): time, session, tool name, decision,
reason, cost, tokens — never the tool's input. Input it cannot use (not
JSON, nested too deeply, no `transcript_path`) or any internal error is
reported and the call goes ahead (exit `1`, a non-blocking hook error),
unless you add `--fail-closed` (exit `2`: the call is blocked). A transcript
line that cannot be parsed is skipped, never a crash. A `--pricing` file
that cannot be loaded does **not** switch the caps off: the built-in table
is used and the warning is added to the decision's reason (or shown once per
session as a `systemMessage`); `hook install` writes the pricing path as an
absolute path and refuses a file it cannot load.

`--fail-closed` only covers failures the hook itself notices. A hook that
cannot start, crashes hard or times out is, by default, a non-blocking error
in Claude Code and the call goes ahead. `hook install --fail-closed`
therefore also writes `"onFailure": "block"` and `"timeout": 60` into the
hook's settings — per the [hooks reference](https://code.claude.com/docs/en/hooks#block-the-action-when-a-hook-fails)
this makes any failure or timeout block the call, and it needs Claude Code
2.1.295 or later. On an older Claude Code that field is not honoured, and a
hook that hangs lets the call through after its timeout (600 s by default).

Hooks inside subagents: the reference does not say whether
`transcript_path` is then the session's transcript or the subagent's own
(`<session>/subagents/agent-<id>.jsonl`). Either way the hook counts the
**whole session** (the main transcript and every subagent's), and loops are
judged on the sequence of the agent the call belongs to (`agent_id`).

Honest limits of hook mode: a hook runs at tool calls only, so a cap is
enforced at the **next tool call** after it is crossed, not mid-answer, and a
turn without tool calls is never stopped. Claude Code writes the transcript
asynchronously, so the newest message may not be counted yet. Only what a
transcript gained since the last call is read (see *Fast hook*). The
transcript layout (assistant lines with `message.id`/`model`/`usage`,
subagents under `<session>/subagents/`) was checked against Claude Code
2.1.318 on 2026-11-05; it is not a documented format. Recent transcripts
also hold `cost-state` lines with Claude Code's own `totalCostUSD`
(undocumented too): when present, the **larger** of that figure and the
estimate is enforced, never the smaller. The `ask` and `deny` outputs follow
the [hooks reference](https://code.claude.com/docs/en/hooks); whether `ask`
prompts in every permission mode (`bypassPermissions`, say) was not tested
here.

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

**Processes that left the group (Linux).** A process group is not a
container: a grandchild that calls `setsid()` (a daemon, `nohup setsid`)
is out of a group kill's reach. On Linux the leash now (a) reads `/proc` at
the moment of the kill and also kills the agent's descendants that are not in
its group, by pid and start time so a recycled pid is never hit; (b) is a
"child subreaper", so processes orphaned below the agent are adopted by the
leash and swept too; and (c) starts a small **sentinel** process in its own
session that holds the other end of a pipe: if the leash dies without
saying it is done (`kill -9`, the OOM killer, a crash) the sentinel kills the
agent's group and every descendant it has been tracking (it looks once a
second). A daemon double-forked less than a second before the leash was
killed with `kill -9` can still escape. Receipts warn when the sweep killed
detached processes. `SHORTLEASH_NO_ESCAPE_SWEEP=1` switches all of it off.
macOS gets only the sentinel's group kill (no `/proc`); it was not run there.
`bench/bench_orphans.py` measures it (*Benchmarks*).

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
| `2` | usage error, command not found, unsafe `.cmd` launch refused, receipt path not writable, a pricing file that cannot be loaded, a spend ledger (`--daily-budget`) or receipt chain (`--chain`) that cannot be used, `fleet list` on a Python without `sqlite3`; for `verify`: the key variable is unset |
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
  pricing:   built-in fallback table as of 2026-11-05 — verify against provider pricing
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

Inside every argument, the prompt included, credential-shaped text is also
replaced by `***`: well-known key formats (`sk-…`, `sk-ant-…`, `ghp_…`,
`github_pat_…`, `glpat-…`, `xox?-…`, `AKIA…`, `AIza…`), `Bearer x` /
`Basic x`, flags and fields named like `auth` (`--auth u:p`,
`Authorization: …`, `X-Api-Key: …`), and secret-named fields in JSON
arguments (`{"api_key": "***"}`). It is pattern matching: a secret in no
known shape and under no telling name is not caught.

### Prices are a hard-coded fallback — verify them

The built-in table (`pricing.py`, USD per 1M tokens) is **not fetched from
anywhere**; it was written on the date in `PRICING_AS_OF`. The startup banner
says so (`pricing: built-in fallback table as of 2026-11-05 — verify against
provider pricing`; not under `--quiet`), the receipt on stderr repeats that
line whenever the cost it shows is an estimate, and the JSON receipt always
carries it (`pricing`: `as_of`, `source`). Check it against the provider's
pricing page before trusting a `--budget` to the cent, and override it with
`--pricing pricing.json` or the `SHORTLEASH_PRICING` environment variable
pointing at a JSON file:

```json
{"as_of": "2026-11-05",
 "sonnet-5-5": [2.0, 10.0, 0.05], "opus-4-5": [5.0, 25.0], "my-model": [1, 4]}
```

`as_of` is optional metadata echoed in the receipt; every other key is a
case-insensitive substring matched against the model name, mapped to
`[input, output]` dollars per 1M tokens, or `[input, output,
cache_read_factor]` for a model whose cache reads are not 10% of its input
rate. The built-in table was copied from
[Anthropic's pricing page](https://platform.claude.com/docs/en/about-claude/pricing)
on 2026-11-05 and names each model version (Opus 5.5 at $4 / $20 with
cache reads at 0.05×, Opus 4.5 at $5 / $25, Opus 4.1 at $15 / $75, …);
cache writes are priced at the 5-minute rate (1.25×), and at 2× for the
part the usage block reports as 1-hour cache writes
(`cache_creation.ephemeral_1h_input_tokens`). Haiku 5.5 is priced per
request by prompt length: up to 100K prompt tokens (input + cache reads +
cache writes) at $0.10 / $0.50, above that at $0.50 / $2.50 (the receipt's
`models` shows those requests as `claude-haiku-5-5#long`).

A table whose `as_of` date is more than 60 days old gets a warning (banner,
receipt, hook reason): prices may have changed. `SHORTLEASH_STALE_DAYS=N`
changes the limit; `0` switches the warning off. The entries of your file are tried
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

## Benchmarks

Three scripts under `bench/` anyone can re-run; each builds its own synthetic
data in a temp directory (no real transcripts, no network, no API calls).
Numbers below: one 2-core Linux VM, Python 3.13, 2026-11-07; yours will differ,
the ratios should not.

**Hook latency** (`python3 bench/bench_hook.py --mb 150 --agents 100`): the
real `python -m shortleash hook` command, one process per tool call, on a
synthetic 151 MB session of 101 transcripts, median of the calls after one
more message was appended.

| | 0.2.0 behaviour (`SHORTLEASH_NO_CACHE=1`) | 0.3.0 |
|---|---|---|
| 40 MB, 41 transcripts | 733 ms | 115 ms |
| 151 MB, 101 transcripts | 2714 ms | 165 ms |

The first call on a session (nothing cached yet) is not faster: 0.9 s and
2.4 s here. (On a real Claude Code session of that shape the full read took
5.7 s per call on the same machine, which is why this matters: the hook's
default timeout is 600 s but `--fail-closed` installs a 60 s one.)

**Processes left behind** (`python3 bench/bench_orphans.py`, Linux): a fake
agent leaves three sleepers, one in its process group, one that called
`setsid()`, one double-forked daemon. Survivors out of 3, sentinel and sweep
off (the 0.2.0 behaviour) against on:

| leash ends by | off | on |
|---|---|---|
| a `--budget` breach | 1 to 2 | 0 |
| `kill -9` | 3 | 0 |
| SIGTERM | 1 to 2 | 0 |

**Stuck agents** (`python3 bench/bench_loops.py`): 8 stuck and 8 healthy
tool-call traces written by the maintainers for this purpose (synthetic, and
written knowing the fuzzy design: it measures the trade-off, it is not an
independent dataset; add your own traces to `CORPUS`). `--max-repeats 4`
throughout.

| setting | stuck traces stopped | healthy traces stopped |
|---|---|---|
| exact (0.2.0) | 1 of 8 | 0 of 8 |
| `--loop-fuzzy` | 6 of 8 | 1 of 8 (a seed sweep printing the same line) |
| `--loop-fuzzy --max-errors 6` | 8 of 8 | 1 of 8 |

## Testing

```bash
python3 -m unittest discover -s tests -v   # or: python3 -m pytest -q
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

## Scope / limits (v0.3.0)

- **What 0.3.0 was run on:** Linux only, Python 3.13, 2 cores. The hook's
  incremental tally was also checked against the figures of one real Claude
  Code 2.1.319 session (the same totals as a full read); `claude -p` was not
  run through `shortleash run`, `UserPromptSubmit` blocking was not tried in a
  live Claude Code, and nothing new was run on macOS or Windows (the
  `flock` / `msvcrt` chain lock and the sentinel are written for them).
- **The estimate can fall short of what Claude Code bills.** On one real
  session with 115 subagent transcripts, the sum of the transcripts, priced
  with the built-in table, came to about 11% below the total Claude Code
  recorded itself (`cost-state`), most of it usage that never reaches a
  transcript (a small background model; cache-write splits). The hook enforces
  the larger of the two, but `cost-state` lines are written only now and then,
  so between them the estimate is what you get. The built-in prices matched
  Claude Code's own per-model dollar figures to the cent for the two models
  that had full usage in the transcripts. Treat every cap as a guardrail with
  a margin.
- `shortleash run` supports Claude Code `-p` (non-interactive); interactive
  Claude Code sessions are covered by `shortleash hook`, which enforces at
  tool calls, not mid-answer (see *Interactive sessions*). Other agents
  (Codex, Cursor) are on the roadmap — the event parser is isolated in
  `events.py` for exactly this.
- **Loop detection is exact-match unless you ask.** `--loop-fuzzy` and
  `--max-errors` (see *Loops and stalls*) catch more, at the price of some
  false positives; none of it catches every way an agent can go round in
  circles (a changed word in a prompt-like argument, a loop that makes
  progress in output every time).
- Cost mid-run is an estimate; treat `--budget` as a guardrail, not accounting.
- **Each message is priced at the model it names** (`message.model`), the
  `system`/`init` model when it names none — so a subagent on a dearer model
  is priced at its own rate (the receipt's `models` lists the live estimate
  per model). Before 0.2.0 everything was priced at the init model's rate.
  It is still an estimate: the agent's own `total_cost_usd` replaces it when
  the run completes and is compared with the caps then. Real Claude Code
  transcripts were seen to name the model on every message (2.1.318); the
  stream-json events are assumed to carry the same `message`.
- **Live enforcement needs per-message events.** An agent that reports only
  a final `result` can be checked after it has finished, not stopped (see
  *Live, or only afterwards*). Whether the real `claude` CLI's
  `--output-format json` has that shape has **not been verified** here.
- **The agent's own report is the evidence.** The leash cannot see spend an
  agent hides or under-reports (see *Trust boundary*); a final report is
  compared with the caps but not audited.
- **SIGKILL of the leash cannot be handled** (no receipt). The sentinel
  (see *Killing the tree*) ends the whole tree on Linux, in about a second;
  macOS gets its group kill only (not run there); Windows relies on the Job
  Object.
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
- **What 0.2.0 was run on:** Linux only, Python 3.13 (the new tests;
  the CI matrix covers the rest). The hook was run against a real Claude
  Code 2.1.318 transcript; `run` was not run against a real `claude`. The
  approval gate's terminal prompt was exercised through a stand-in (no
  terminal in the test machine); pausing the tree was checked on Linux.
- **What 0.1.3 was run on:** Linux only, Python 3.10 to 3.14 (and pytest on
  3.11). Not on Windows or macOS, and not against a real `claude`. Agent names
  in other letter cases are tested by name (and with a script really called
  `Claude` on POSIX), a Python without `sqlite3` is simulated, and the
  byte-order-mark and UTF-16 files are built byte by byte, not written by a
  real PowerShell.
