# Changelog

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

Changed:
- Each message is priced at the model it names (a subagent on another model
  at its own rate), not all at the `system/init` model's rate.
- The built-in pricing table names model versions and was refreshed from
  Anthropic's pricing page on 2026-11-05 (e.g. Opus 4.5 is $5 / $25, not
  the $15 / $75 the old generic "opus" entry used); entries may give a cache
  read factor (`[in, out, factor]`) for models whose cache reads are not 10%.
- A breach kill also sends SIGCONT to the group, so a stopped process acts on
  SIGTERM at once.
