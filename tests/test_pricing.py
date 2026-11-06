import json
import os
import tempfile
import unittest

from shortleash import pricing


class TestPricing(unittest.TestCase):
    def test_sonnet_estimate(self):
        usage = {"input": 1_000_000, "output": 1_000_000,
                 "cache_write": 0, "cache_read": 0}
        cost = pricing.estimate_cost("claude-sonnet-4-5", usage)
        self.assertAlmostEqual(cost, 18.00)  # 3 + 15

    def test_cache_factors(self):
        usage = {"input": 0, "output": 0,
                 "cache_write": 1_000_000, "cache_read": 1_000_000}
        cost = pricing.estimate_cost("claude-sonnet-4-5", usage)
        # write: 3*1.25=3.75 ; read: 3*0.10=0.30
        self.assertAlmostEqual(cost, 4.05)

    def test_model_matching(self):
        usage = {"input": 1_000_000, "output": 0,
                 "cache_write": 0, "cache_read": 0}
        self.assertAlmostEqual(
            pricing.estimate_cost("claude-opus-4-1", usage), 15.00)
        self.assertAlmostEqual(
            pricing.estimate_cost("claude-haiku-4", usage), 0.80)

    def test_unknown_model_falls_back_to_default(self):
        usage = {"input": 1_000_000, "output": 0,
                 "cache_write": 0, "cache_read": 0}
        self.assertAlmostEqual(
            pricing.estimate_cost("mystery-model-9", usage), 3.00)

    def test_load_table_override(self):
        with tempfile.NamedTemporaryFile("w", suffix=".json",
                                         delete=False) as f:
            json.dump({"sonnet": [10.0, 20.0]}, f)
            path = f.name
        try:
            table = pricing.load_table(path, environ={})
            usage = {"input": 1_000_000, "output": 0,
                     "cache_write": 0, "cache_read": 0}
            self.assertAlmostEqual(
                pricing.estimate_cost("claude-sonnet-4-5", usage, table), 10.00)
            # a plain rates dict still works for estimate_cost; it has no
            # notion of "the user's entries first", so the longest key of
            # the merged table wins there (the built-in "sonnet-4")
            self.assertAlmostEqual(
                pricing.estimate_cost("claude-sonnet-4-5", usage, table.rates),
                3.00)
            self.assertAlmostEqual(
                pricing.estimate_cost("claude-sonnet-9", usage, table.rates),
                10.00)
        finally:
            os.unlink(path)

    def test_verified_rates_and_cache_read_factors(self):
        # platform.claude.com/docs/en/about-claude/pricing, 2026-11-05
        one = {"input": 1_000_000, "output": 1_000_000,
               "cache_write": 1_000_000, "cache_read": 1_000_000}
        # Opus 5.5: 4 + 20 + 5 (write 1.25x) + 0.20 (read 0.05x)
        self.assertAlmostEqual(pricing.estimate_cost("claude-opus-5-5", one),
                               29.20)
        # Opus 4.5: 5 + 25 + 6.25 + 0.50
        self.assertAlmostEqual(pricing.estimate_cost("claude-opus-4-5", one),
                               36.75)
        # Haiku 4.5: 1 + 5 + 1.25 + 0.10
        self.assertAlmostEqual(pricing.estimate_cost("claude-haiku-4-5", one),
                               7.35)
        # Fable 5.1 reads at 0.025x: 10 + 50 + 12.5 + 0.25
        self.assertAlmostEqual(pricing.estimate_cost("claude-fable-5-1", one),
                               72.75)

    def test_a_file_can_give_a_cache_read_factor(self):
        with tempfile.NamedTemporaryFile("w", suffix=".json",
                                         delete=False) as f:
            json.dump({"mymodel": [1.0, 2.0, 0.5]}, f)
            path = f.name
        try:
            table = pricing.load_table(path, environ={})
            usage = {"input": 0, "output": 0, "cache_write": 0,
                     "cache_read": 1_000_000}
            self.assertAlmostEqual(
                pricing.estimate_cost("mymodel-x", usage, table), 0.5)
        finally:
            os.unlink(path)
        for bad in ([1, 2, 3, 4], [1, 2, "x"], [1, 2, -1]):
            with tempfile.NamedTemporaryFile("w", suffix=".json",
                                             delete=False) as f:
                json.dump({"m": bad}, f)
                path = f.name
            try:
                with self.assertRaises(ValueError):
                    pricing.load_table(path, environ={})
            finally:
                os.unlink(path)

    def test_load_table_none_returns_defaults(self):
        self.assertEqual(pricing.load_table(None, environ={}).rates,
                         pricing.DEFAULT_PRICING)


if __name__ == "__main__":
    unittest.main()
