"""Panel round 3: no false comfort without telemetry, and labelled prices.

- A child that never emits stream-json usage events gives the guard
  nothing to enforce a budget/token/spawn cap against. The receipt must
  say so plainly ("no usage data seen — only the wall-clock/time limit
  was enforced"), the cost must be n/a (not $0.0000), the green tick
  must not appear, and the exit code must be 125 when such a cap was
  requested — never imply a cost cap held.
- The hard-coded pricing table carries an as_of date, is overridable
  from a JSON file (--pricing) or $SHORTLEASH_PRICING, and the receipt
  prints the as_of date with "verify against provider pricing".
"""

import _isolate  # noqa: F401  (isolate ~/.shortleash before importing)

import io
import json
import os
import re
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

from shortleash import cli as cli_mod
from shortleash import pricing
from shortleash import receipt as receipt_mod
from shortleash.guard import Guard, NO_TELEMETRY_EXIT_CODE, agent_name

PY = sys.executable
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SIM = os.path.join(REPO, "sim", "runaway.py")

SILENT_CHILD = [PY, "-c", "print('hello from a tool with no telemetry')"]


def _run(guard: Guard, argv: list[str]) -> tuple[int, dict, str]:
    tmp = tempfile.mkdtemp()
    guard.receipt_path = os.path.join(tmp, "r.json")
    guard.history_path = os.path.join(tmp, "h.jsonl")
    err = io.StringIO()
    with redirect_stderr(err), redirect_stdout(io.StringIO()):
        code = guard.run(argv)
    with open(guard.receipt_path, encoding="utf-8") as f:
        rec = json.load(f)
    return code, rec, err.getvalue()


class TestNoTelemetry(unittest.TestCase):
    def test_budget_without_telemetry_is_reported_and_exit_125(self):
        g = Guard(budget=2.0, timeout=30.0, quiet=True, anomaly=None)
        code, rec, _ = _run(g, SILENT_CHILD)
        self.assertEqual(code, NO_TELEMETRY_EXIT_CODE)
        self.assertEqual(rec["exit_code"], NO_TELEMETRY_EXIT_CODE)
        self.assertFalse(rec["telemetry_seen"])
        self.assertEqual(rec["usage_events"], 0)
        self.assertIsNone(rec["cost_usd"], "no data => no cost figure")
        self.assertEqual(rec["enforced"], ["timeout"])
        self.assertEqual(rec["not_enforced"], ["budget"])
        self.assertFalse(rec["breached"])
        text = receipt_mod.format_human(rec)
        self.assertIn("no usage data seen — only the wall-clock/time limit "
                      "was enforced", text)
        self.assertIn("NOT enforced: budget", text)
        self.assertNotIn("✓", text)
        self.assertNotIn("$0.0000", text)
        self.assertIn("n/a", text)

    def test_all_telemetry_caps_listed_as_not_enforced(self):
        g = Guard(budget=1.0, max_tokens=10, max_spawns=1, quiet=True,
                  anomaly=None)
        code, rec, _ = _run(g, SILENT_CHILD)
        self.assertEqual(code, NO_TELEMETRY_EXIT_CODE)
        self.assertEqual(rec["not_enforced"], ["budget", "max_tokens",
                                               "max_spawns"])
        self.assertEqual(rec["enforced"], [])
        text = receipt_mod.format_human(rec)
        # no --timeout either: nothing at all was enforced, say so
        self.assertIn("nothing was enforced", text)

    def test_timeout_only_keeps_child_exit_code(self):
        # Only the wall-clock limit was asked for: it WAS enforced, so the
        # child's own exit code stands — but the receipt still says no
        # usage data was seen.
        g = Guard(timeout=30.0, quiet=True, anomaly=None)
        code, rec, _ = _run(g, SILENT_CHILD)
        self.assertEqual(code, 0)
        self.assertFalse(rec["telemetry_seen"])
        self.assertEqual(rec["not_enforced"], [])
        self.assertIn("no usage data seen", receipt_mod.format_human(rec))
        self.assertNotIn("✓", receipt_mod.format_human(rec))

    def test_failing_child_exit_code_wins_over_125(self):
        g = Guard(budget=1.0, quiet=True, anomaly=None)
        code, rec, _ = _run(g, [PY, "-c", "raise SystemExit(3)"])
        self.assertEqual(code, 3)
        self.assertFalse(rec["telemetry_seen"])
        self.assertEqual(rec["not_enforced"], ["budget"])

    def test_telemetry_less_runs_not_recorded_in_history(self):
        g = Guard(budget=1.0, quiet=True, anomaly=None)
        _run(g, SILENT_CHILD)
        self.assertFalse(os.path.exists(g.history_path),
                         "a run with no usage data must not seed baselines")

    def test_sim_with_telemetry_is_unaffected(self):
        g = Guard(budget=10.0, quiet=True, anomaly=None)
        code, rec, _ = _run(g, [PY, SIM, "--well-behaved"])
        self.assertEqual(code, 0)
        self.assertTrue(rec["telemetry_seen"])
        self.assertGreater(rec["usage_events"], 0)
        self.assertEqual(rec["enforced"], ["budget"])
        self.assertEqual(rec["not_enforced"], [])
        text = receipt_mod.format_human(rec)
        self.assertIn("finished within guardrails ✓", text)
        self.assertNotIn("no usage data", text)

    def test_cli_end_to_end_exit_125(self):
        tmp = tempfile.mkdtemp()
        rp = os.path.join(tmp, "r.json")
        err = io.StringIO()
        with redirect_stderr(err), redirect_stdout(io.StringIO()):
            code = cli_mod.main(["run", "--budget", "1", "--no-anomaly",
                                 "--history-path", os.path.join(tmp, "h"),
                                 "--receipt", rp, "--quiet", "--"]
                                + SILENT_CHILD)
        self.assertEqual(code, NO_TELEMETRY_EXIT_CODE)
        with open(rp, encoding="utf-8") as f:
            self.assertFalse(json.load(f)["telemetry_seen"])


class TestPricingLabels(unittest.TestCase):
    def test_as_of_is_an_iso_date(self):
        self.assertRegex(pricing.PRICING_AS_OF, r"^\d{4}-\d{2}-\d{2}$")
        t = pricing.load_table(None, environ={})
        self.assertEqual(t.as_of, pricing.PRICING_AS_OF)
        self.assertEqual(t.rates, pricing.DEFAULT_PRICING)
        self.assertIn("verify against provider pricing", t.describe())
        self.assertIn(pricing.PRICING_AS_OF, t.describe())

    def _write(self, data: dict) -> str:
        fd, path = tempfile.mkstemp(suffix=".json")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f)
        return path

    def test_file_override_with_as_of(self):
        path = self._write({"as_of": "2027-01-02", "sonnet": [10.0, 20.0]})
        t = pricing.load_table(path, environ={})
        self.assertEqual(t.as_of, "2027-01-02")
        self.assertEqual(t.rates["sonnet"], (10.0, 20.0))
        self.assertEqual(t.rates["opus"], pricing.DEFAULT_PRICING["opus"])
        self.assertEqual(t.source, path)
        # the legacy loader must not misread "as_of" as a [2, 0] rate pair
        table = pricing.load_pricing(path)
        self.assertNotIn("as_of", table)
        self.assertEqual(table["sonnet"], (10.0, 20.0))

    def test_env_var_override(self):
        path = self._write({"as_of": "2027-03-04", "haiku": [1.0, 2.0]})
        t = pricing.load_table(None, environ={pricing.PRICING_ENV: path})
        self.assertEqual(t.as_of, "2027-03-04")
        self.assertEqual(t.rates["haiku"], (1.0, 2.0))
        self.assertIn(pricing.PRICING_ENV, t.source)
        # --pricing beats the env var
        other = self._write({"haiku": [5.0, 6.0]})
        t2 = pricing.load_table(other, environ={pricing.PRICING_ENV: path})
        self.assertEqual(t2.rates["haiku"], (5.0, 6.0))
        self.assertIn("no as_of", t2.as_of)

    def test_bad_rate_shape_rejected(self):
        for bad in ({"sonnet": "3.0"}, {"sonnet": [3.0]}, {"sonnet": ["3", "15"]},
                    {"sonnet": [True, 1.0]}, ["not", "a", "dict"]):
            path = self._write(bad)
            with self.assertRaises(ValueError, msg=repr(bad)):
                pricing.load_table(path, environ={})

    def test_receipt_prints_as_of(self):
        g = Guard(budget=10.0, quiet=True, anomaly=None)
        code, rec, _ = _run(g, [PY, SIM, "--turns", "2", "--tokens-per-turn",
                                "100", "--spawns", "0", "--delay", "0"])
        self.assertEqual(rec["pricing"]["as_of"], pricing.PRICING_AS_OF)
        text = receipt_mod.format_human(rec)
        # the sim reports its own final cost, so the pricing line is only
        # printed while the cost is an estimate: force that case
        rec["cost_estimated"] = True
        text = receipt_mod.format_human(rec)
        self.assertIn(f"as of {pricing.PRICING_AS_OF}", text)
        self.assertIn("verify against provider pricing", text)

    def test_startup_banner_names_pricing_as_of(self):
        err = io.StringIO()
        tmp = tempfile.mkdtemp()
        with redirect_stderr(err), redirect_stdout(io.StringIO()):
            cli_mod.main(["run", "--no-anomaly", "--timeout", "30",
                          "--history-path", os.path.join(tmp, "h"), "--"]
                         + SILENT_CHILD)
        self.assertIn(f"as of {pricing.PRICING_AS_OF}", err.getvalue())

    def test_cli_env_pricing_error_is_clean(self):
        with mock.patch.dict(os.environ,
                             {pricing.PRICING_ENV: os.path.join(
                                 tempfile.mkdtemp(), "nope.json")}):
            err = io.StringIO()
            with redirect_stderr(err):
                code = cli_mod.main(["run", "--no-anomaly", "--"] + SILENT_CHILD)
        self.assertEqual(code, 2)
        self.assertIn("pricing", err.getvalue())
        self.assertNotIn("Traceback", err.getvalue())


class TestAgentName(unittest.TestCase):
    def test_strips_windows_suffixes_and_paths(self):
        self.assertEqual(agent_name("claude"), "claude")
        self.assertEqual(agent_name("/usr/local/bin/claude"), "claude")
        self.assertEqual(agent_name(r"C:\Users\dan\AppData\Roaming\npm\claude.cmd"),
                         "claude")
        self.assertEqual(agent_name("C:/Python311/python.exe"), "python")
        self.assertEqual(agent_name(".exe"), ".exe")

    def test_stream_json_injected_for_claude_cmd(self):
        out = cli_mod._maybe_inject_stream_json([r"C:\npm\claude.CMD", "-p", "x"])
        self.assertEqual(out[-3:], ["--output-format", "stream-json", "--verbose"])


if __name__ == "__main__":
    unittest.main()
