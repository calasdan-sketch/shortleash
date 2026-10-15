"""Guard integration tests against the fake-agent simulator.

These run the real Guard (real subprocess, real kill signals) against
sim/runaway.py, which emits Claude Code stream-json without spending
a cent.
"""

import json
import io
import os
import sys
import tempfile
import types
import unittest
from contextlib import redirect_stdout

from shortleash.guard import Guard, BREACH_EXIT_CODE

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SIM = os.path.join(REPO, "sim", "runaway.py")
PY = sys.executable


def sim_cmd(*args: str) -> list[str]:
    return [PY, SIM, *args]


class TestGuard(unittest.TestCase):
    def _run_guard(self, guard: Guard, args: list[str]) -> tuple[int, dict]:
        with tempfile.NamedTemporaryFile("w", suffix=".json",
                                         delete=False) as f:
            receipt_path = f.name
        guard.receipt_path = receipt_path
        # Isolate history: never touch the developer's real ~/.shortleash.
        tmpdir = tempfile.mkdtemp()
        guard.history_path = os.path.join(tmpdir, "h.jsonl")
        try:
            code = guard.run(sim_cmd(*args))
            with open(receipt_path) as f:
                rec = json.load(f)
            return code, rec
        finally:
            os.unlink(receipt_path)

    def test_well_behaved_run_passes(self):
        g = Guard(budget=10.0, max_spawns=5, quiet=True)
        code, rec = self._run_guard(g, ["--well-behaved"])
        self.assertEqual(code, 0)
        self.assertFalse(rec["breached"])
        self.assertIsNone(rec["breach_reason"])
        # Simulator reports its own cost; receipt must prefer it.
        self.assertAlmostEqual(rec["cost_usd"], 0.012)
        self.assertFalse(rec["cost_estimated"])
        self.assertEqual(rec["num_turns"], 2)

    def test_budget_breach_kills_run(self):
        g = Guard(budget=0.05, quiet=True, grace_period=1.0)
        code, rec = self._run_guard(
            g, ["--turns", "30", "--tokens-per-turn", "15000",
                "--delay", "0.05"])
        self.assertEqual(code, BREACH_EXIT_CODE)
        self.assertTrue(rec["breached"])
        self.assertIn("budget exceeded", rec["breach_reason"])
        # Killed early: far fewer than 30 turns of tokens accrued.
        self.assertLess(rec["tokens"]["input"], 30 * 15000)

    def test_spawn_cap_breach(self):
        g = Guard(max_spawns=2, quiet=True, grace_period=1.0)
        code, rec = self._run_guard(
            g, ["--turns", "20", "--spawns", "8", "--delay", "0.05"])
        self.assertEqual(code, BREACH_EXIT_CODE)
        self.assertTrue(rec["breached"])
        self.assertIn("spawn cap exceeded", rec["breach_reason"])
        self.assertGreater(rec["subagent_spawns"], 2)

    def test_token_cap_breach(self):
        g = Guard(max_tokens=10_000, quiet=True, grace_period=1.0)
        code, rec = self._run_guard(
            g, ["--turns", "20", "--tokens-per-turn", "15000",
                "--delay", "0.05"])
        self.assertEqual(code, BREACH_EXIT_CODE)
        self.assertTrue(rec["breached"])
        self.assertIn("token cap exceeded", rec["breach_reason"])

    def test_timeout_breach(self):
        g = Guard(timeout=1.0, quiet=True, grace_period=1.0)
        code, rec = self._run_guard(
            g, ["--turns", "60", "--delay", "0.5"])
        self.assertEqual(code, BREACH_EXIT_CODE)
        self.assertTrue(rec["breached"])
        self.assertIn("time limit exceeded", rec["breach_reason"])
        self.assertLess(rec["duration_s"], 10)

    def test_no_caps_means_no_breach(self):
        g = Guard(quiet=True)
        code, rec = self._run_guard(
            g, ["--turns", "5", "--tokens-per-turn", "1000",
                "--spawns", "1", "--delay", "0.02"])
        self.assertEqual(code, 0)
        self.assertFalse(rec["breached"])
        self.assertEqual(rec["subagent_spawns"], 1)

    def test_negative_telemetry_clamped_to_zero(self):
        # A misbehaving child reporting negative usage/cost must not
        # drive totals down and blind the caps.
        g = Guard(quiet=True, anomaly=None)
        g._add_usage({"usage": {"input": -5000, "output": 100,
                                "cache_write": -3, "cache_read": 0},
                      "spawns": -2})
        self.assertEqual(g.usage["input"], 0)
        self.assertEqual(g.usage["output"], 100)
        self.assertEqual(g.usage["cache_write"], 0)
        self.assertEqual(g.spawns, 0)

    def test_negative_reported_cost_clamped_to_zero(self):
        g = Guard(quiet=True, anomaly=None)
        line = json.dumps({"type": "result", "subtype": "success",
                           "is_error": False, "total_cost_usd": -9.99,
                           "num_turns": 1, "session_id": "x",
                           "usage": {"input_tokens": -5}}) + "\n"
        g.proc = types.SimpleNamespace(stdout=io.StringIO(line),
                                       poll=lambda: 0)
        buf = io.StringIO()
        with redirect_stdout(buf):
            g._monitor_stdout()
        self.assertEqual(g.reported_cost, 0.0)


if __name__ == "__main__":
    unittest.main()
