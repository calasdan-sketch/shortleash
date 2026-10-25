# Changelog

## 0.1.4

- `run --fleet`: if stderr is closed early (`... 2>&1 | head -1`) the agent now still starts and the fleet claim is released, instead of leaking the claim and never running.
- An agent printing one enormous output line no longer balloons memory: lines over 8 MB are skipped (with a warning in the receipt) instead of being buffered whole.
- Receipts redact more secrets: `Authorization`/`Cookie` headers (`-H`/`--header`), `--auth`, `--bearer`, `--credential`, `--cookie`, `-u`/`--user`, and bare `sk-...`, `Bearer ...`, GitHub, Slack and AWS tokens. Receipt files are written with mode 0600.
- Built-in price table updated to Anthropic's published rates (Opus 4.5 and later $5/$25, Haiku 4.5 $1/$5, Sonnet 5 $2/$10, plus Opus 4.1/4 at $15/$75 and Haiku 3.5 at $0.80/$4). `PRICING_AS_OF` is now 2026-10-24.
- `python -m unittest discover -s tests -t .` works as well as `-s tests`.
- Package metadata: authors, changelog URL, Python 3.14 classifier. `pip install shortleash` is the documented install.

## 0.1.3

- Previous release.
