# Changelog

## 0.3.0 — unreleased

Found by putting the tool in front of FinOps, process-supervision, reliability
and agent-safety testers (and a first-time user reading only the README); the
numbers are from `bench/` and can be re-run.

New:
- **Fast hook.** The hook keeps the state after the bytes it has read of each
  transcript (`<state>/hook-cache/`) and reads only what was appended. On a
  synthetic 151 MB / 101-transcript session a call went from 2714 ms to
  165 ms (40 MB: 733 ms to 115 ms). Same figures as a full read (tested by
  replaying random growth); a stale, corrupt or replaced cache is dropped.
  `SHORTLEASH_NO_CACHE=1` switches it off. A transcript line over 32 MB is
  skipped, not held in memory.
- **Spend ledger and machine-wide caps:** `--daily-budget USD` and
  `--project-daily-budget USD` for `run` and `hook`, over every run and
  session on the machine for the local day. One append-only file per run /
  session, no locks, no lost writes under concurrency (tested with 6
  processes). Unusable ledger: `run` exits `2` before starting, the hook
  fails per `--fail-closed`. Receipts carry `ledger`.
- **Tamper-evident receipts:** `run --chain PATH` appends each receipt to a
  hash-chained JSON-lines log; `shortleash verify PATH` names the first
  edited, removed, inserted or moved record. Optional HMAC
  (`--chain-key-env` / `SHORTLEASH_CHAIN_KEY`) and `--expect-head` to notice
  cut-off records. Exclusive lock for concurrent runs.
- **`hook install --block-prompts`:** also registers the hook on
  `UserPromptSubmit`, so a session over its budget cannot take another
  prompt (a talking-only turn never reaches `PreToolUse`). The prompt is
  never logged.
- **`--loop-fuzzy`:** loop matching that ignores white space and numbers in
  inputs and timestamps, UUIDs, hex ids, durations and pids in results.
  `--max-errors N`: stop after N tool calls in a row failed (`run` and
  `hook`). On the bundled synthetic corpus: 1 of 8 stuck traces caught
  before, 6 with fuzzy, 8 with both; one false positive (a seed sweep).
- **Processes that leave the group (Linux):** at a kill the leash also kills
  the agent's descendants that called `setsid()` (found through `/proc`, by
  pid and start time), is a child subreaper so orphans below the agent are
  swept, and runs a **sentinel** that kills the agent's group and every
  tracked descendant if the leash itself dies without a goodbye (`kill -9`,
  the OOM killer). Survivors out of 3 left-behind sleepers: `kill -9` 3 to 0,
  breach 1 to 2 to 0, SIGTERM 1 to 2 to 0. `SHORTLEASH_NO_ESCAPE_SWEEP=1`
  switches it off.
- `bench/bench_hook.py`, `bench/bench_orphans.py`, `bench/bench_loops.py`;
  `sim/orphans.py`.

Changed:
- Comparison table in the README rebuilt (Anthropic spend limits, ccusage,
  LiteLLM, AgentBudget) with sources, and a *Benchmarks* section.
- `events.parse_line`, `loops.fingerprint` and `loops.result_digest` take an
  optional `fuzzy` flag; tool-result observations carry `errors`.
  Receipts have a new `ledger` field (null unless a daily cap was set).
  Everything else is backward compatible.

Known limits, stated in the README: the estimate can sit about 11% under what
Claude Code records itself on a subagent-heavy session (the hook enforces
the larger of the two when `cost-state` is present); a daemon double-forked
less than a second before a `kill -9` can still escape; fuzzy matching and
`--max-errors` trade some false positives for recall; nothing new was run on
macOS or Windows; `UserPromptSubmit` blocking was not tried in a live Claude
Code.

## 0.2.0 — unreleased

New:
- `--max-repeats N`: stops a run whose agent makes the same tool call (same
  tool, same input) N times in a row, or repeats the same cycle of two or
  three calls N times. The reason names the tool and a hash of its input,
  never the input.
- `--idle-timeout SECONDS`: stops a run that has printed nothing for that
  long (stuck), independent of telemetry.
- `--ask-over USD` / `--ask-timeout`: an approval gate. Past the threshold
  the agent's process tree is paused and a human is asked on the terminal
  whether it may continue up to `--budget`; no answer, no terminal or no way
  to pause (Windows) stops the run. The receipt records the answer.
- `shortleash hook`: a Claude Code PreToolUse hook for interactive sessions
  with `--budget`, `--max-tokens`, `--max-spawns`, `--max-repeats` and an
  `--ask-over` gate that uses Claude Code's own permission prompt. Reads the
  session and subagent transcripts; logs each deny/ask to
  `hook-log.jsonl`. `shortleash hook install [--write settings.json]` sets
  it up.
- Spend forecast: one line at 50% and 80% of `--budget` with the rate and
  the time to the cap (`--warn-at`). Receipts carry
  `burn_rate_usd_per_min`, `budget_used_pct`, `models` and `approval`.
- `--receipt-md PATH`: the receipt as Markdown, appended (for
  `$GITHUB_STEP_SUMMARY`); command and prompt left out, agent text escaped.

Review fixes (panel round on 0.2.0):
- The hook never crashes on deeply nested JSON (stdin or transcript); any
  internal error exits 2 under `--fail-closed`. `hook install --fail-closed`
  writes `"onFailure": "block"` and a 60 s timeout (Claude Code 2.1.295+).
- An unloadable `--pricing` file in the hook falls back to the built-in
  prices with a warning instead of disabling the caps; `hook install` makes
  the path absolute and checks it.
- Loop detection compares results when known, exempts polling tools,
  looks for cycles of up to 4 calls and ignores trailing white space.
- Haiku 5.5 priced per request by prompt length; 1-hour cache writes at 2x.
- A warning when the price table is more than 60 days old
  (`SHORTLEASH_STALE_DAYS`).
- Receipts scrub credential-shaped strings, `Bearer` tokens, `--auth` and
  secret-named JSON/header fields in every argument.
- A hook given a subagent's transcript still counts the whole session.

Changed:
- Each message is priced at the model it names (a subagent on another model
  at its own rate), not all at the `system/init` model's rate.
- The built-in pricing table names model versions and was refreshed from
  Anthropic's pricing page on 2026-11-05 (e.g. Opus 4.5 is $5 / $25, not
  the $15 / $75 the old generic "opus" entry used); entries may give a cache
  read factor (`[in, out, factor]`) for models whose cache reads are not 10%.
- A breach kill also sends SIGCONT to the group, so a stopped process acts on
  SIGTERM at once.
