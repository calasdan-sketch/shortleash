# Changelog

## 0.2.0

- Spawn-storm guard: `--max-spawns` now explains itself. When it trips, the receipt says what to do and that the run stops quickly, not instantly.
- The receipt names the culprit: `38 spawns, 71% of tokens in agent Explore` (new `culprit` and `agents` fields in the JSON receipt).
- `.shortleash.toml`: per-repo limits (budget, max_spawns, max_tokens, timeout, forecast, profile). Flags win over the file. `--config`, `--no-config`.
- Early-warning forecast at 20%, 50% and 80% of a budget or token cap, at the pace so far. `--no-forecast` turns it off.
- `shortleash hook install | uninstall | status`: a Claude Code PreToolUse hook that denies the subagent start past a per-session cap, and a SubagentStart hook that records agent types. Your own hooks are never changed. It fails open and cannot stop a running subagent; the README lists what it cannot do.

## 0.1.5

- Release workflow: the tag check read a version from `pyproject.toml` that does not exist here (the version is dynamic), so a correct tag failed. The check now compares the tag with the version in the built file names (`.github/scripts/release_check.py`), and refuses a tag whose commit is not on `main`.
- CI builds the source archive and runs the tests from the unpacked archive.
- Receipts no longer mangle ordinary words: `disk-usage-report` and `basic functionality` in a prompt are left alone, and `--author` is not treated as a secret (`auth` now matches only as a whole word: `--auth`, `--oauth`, `--authorization`).
- Receipts redact more: `-uuser:pass`, `https://user:pass@host/x` (the `user:pass@` part) and `password:value`.
- README: the 8 million character line skip, the echo queue counting pieces rather than lines, `claude-3-haiku` being priced as Haiku 4.5, and 1-hour cache writes not being modelled are now written down.

## 0.1.4

- `run --fleet`: if stderr is closed early (`... 2>&1 | head -1`) the agent now still starts and the fleet claim is released, instead of leaking the claim and never running.
- An agent printing one enormous output line no longer balloons memory: lines over 8 MB are skipped (with a warning in the receipt) instead of being buffered whole.
- Receipts redact more secrets: `Authorization`/`Cookie` headers (`-H`/`--header`), `--auth`, `--bearer`, `--credential`, `--cookie`, `-u`/`--user`, and bare `sk-...`, `Bearer ...`, GitHub, Slack and AWS tokens. Receipt files are written with mode 0600.
- Built-in price table updated to Anthropic's published rates (Opus 4.5 and later $5/$25, Haiku 4.5 $1/$5, Sonnet 5 $2/$10, plus Opus 4.1/4 at $15/$75 and Haiku 3.5 at $0.80/$4). `PRICING_AS_OF` is now 2026-10-24.
- `python -m unittest discover -s tests -t .` works as well as `-s tests`.
- Package metadata: authors, changelog URL, Python 3.14 classifier. `pip install shortleash` is the documented install.

## 0.1.3

- Previous release.
