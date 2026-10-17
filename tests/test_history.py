"""History baselines + anomaly detection tests."""

import _isolate  # noqa: F401  (isolate ~/.shortleash before importing)

import json
import os
import sys
import tempfile
import unittest

from shortleash import history as history_mod
from shortleash.guard import Guard, BREACH_EXIT_CODE, agent_name

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SIM = os.path.join(REPO, "sim", "runaway.py")
PY = sys.executable
AGENT = agent_name(PY)  # what Guard derives from argv[0] (no .exe on Windows)


def seed_history(path: str, agent: str, costs: list[float],
                 tokens: int = 1000, breached: bool = False) -> None:
    for c in costs:
        history_mod.record({
            "agent": agent, "profile": "",
            "cost_usd": c,
            "tokens": {"input": tokens, "output": 0,
                       "cache_write": 0, "cache_read": 0},
            "breached": breached,
            "ended_at": "2026-10-15T00:00:00+00:00",
        }, path)


class TestHistory(unittest.TestCase):
    def test_baseline_needs_min_runs(self):
        tmp = tempfile.mkdtemp()
        p = os.path.join(tmp, "h.jsonl")
        seed_history(p, "claude", [0.1, 0.2, 0.15])
        self.assertIsNone(history_mod.baseline("claude", path=p, min_runs=5))
        b = history_mod.baseline("claude", path=p, min_runs=3)
        self.assertIsNotNone(b)
        self.assertEqual(b["runs"], 3)

    def test_baseline_median(self):
        tmp = tempfile.mkdtemp()
        p = os.path.join(tmp, "h.jsonl")
        seed_history(p, "claude", [0.1, 0.2, 0.3, 0.4, 10.0])
        b = history_mod.baseline("claude", path=p)
        self.assertAlmostEqual(b["median_cost"], 0.3)

    def test_breached_runs_excluded(self):
        tmp = tempfile.mkdtemp()
        p = os.path.join(tmp, "h.jsonl")
        seed_history(p, "claude", [0.1, 0.2, 0.15, 0.12, 0.18])
        seed_history(p, "claude", [50.0], breached=True)
        b = history_mod.baseline("claude", path=p)
        self.assertEqual(b["runs"], 5)
        self.assertAlmostEqual(b["median_cost"], 0.15)

    def test_profile_grouping(self):
        tmp = tempfile.mkdtemp()
        p = os.path.join(tmp, "h.jsonl")
        seed_history(p, "claude", [0.1] * 5)
        history_mod.record({"agent": "claude", "profile": "big-refactor",
                            "cost_usd": 5.0,
                            "tokens": {"input": 1, "output": 0,
                                       "cache_write": 0, "cache_read": 0},
                            "breached": False, "ended_at": ""}, p)
        b = history_mod.baseline("claude", profile="big-refactor", path=p,
                                 min_runs=1)
        self.assertAlmostEqual(b["median_cost"], 5.0)
        b2 = history_mod.baseline("claude", path=p, min_runs=1)
        self.assertAlmostEqual(b2["median_cost"], 0.1)

    def test_missing_file_returns_none(self):
        self.assertIsNone(history_mod.baseline(
            "claude", path="/nope/missing.jsonl"))


class TestAnomalyGuard(unittest.TestCase):
    def test_anomaly_trips_on_runaway(self):
        tmp = tempfile.mkdtemp()
        hist = os.path.join(tmp, "h.jsonl")
        # Seed: this "agent" (python sim) normally costs ~$0.01.
        seed_history(hist, AGENT,
                     [0.010, 0.012, 0.009, 0.011, 0.013], tokens=2000)
        g = Guard(anomaly=3.0, history_path=hist, quiet=True,
                  grace_period=1.0)
        code = g.run([PY, SIM, "--turns", "20", "--tokens-per-turn", "15000",
                      "--delay", "0.02"])
        self.assertEqual(code, BREACH_EXIT_CODE)
        self.assertTrue(g.breached)
        self.assertIn("anomaly", g.breach_reason)

    def test_normal_run_does_not_trip(self):
        tmp = tempfile.mkdtemp()
        hist = os.path.join(tmp, "h.jsonl")
        seed_history(hist, AGENT,
                     [0.010, 0.012, 0.009, 0.011, 0.013], tokens=2000)
        g = Guard(anomaly=3.0, history_path=hist, quiet=True)
        code = g.run([PY, SIM, "--well-behaved"])
        self.assertEqual(code, 0)
        self.assertFalse(g.breached)

    def test_no_history_no_anomaly(self):
        tmp = tempfile.mkdtemp()
        hist = os.path.join(tmp, "empty.jsonl")
        g = Guard(anomaly=3.0, history_path=hist, quiet=True)
        code = g.run([PY, SIM, "--turns", "5", "--tokens-per-turn", "1000",
                      "--delay", "0.02"])
        self.assertEqual(code, 0)  # anomaly inert without history
        self.assertFalse(g.breached)

    def test_no_anomaly_flag(self):
        tmp = tempfile.mkdtemp()
        hist = os.path.join(tmp, "h.jsonl")
        seed_history(hist, AGENT,
                     [0.010, 0.012, 0.009, 0.011, 0.013], tokens=2000)
        g = Guard(anomaly=None, history_path=hist, quiet=True,
                  grace_period=1.0)
        code = g.run([PY, SIM, "--turns", "20", "--tokens-per-turn", "15000",
                      "--delay", "0.02"])
        self.assertEqual(code, 0)
        self.assertFalse(g.breached)

    def test_runs_recorded_to_history(self):
        tmp = tempfile.mkdtemp()
        hist = os.path.join(tmp, "h.jsonl")
        g = Guard(history_path=hist, quiet=True, min_history=100)
        g.run([PY, SIM, "--well-behaved"])
        with open(hist, encoding="utf-8") as f:
            lines = [json.loads(l) for l in f if l.strip()]
        self.assertEqual(len(lines), 1)
        self.assertEqual(lines[0]["agent"], AGENT)
        self.assertFalse(lines[0]["breached"])


if __name__ == "__main__":
    unittest.main()
