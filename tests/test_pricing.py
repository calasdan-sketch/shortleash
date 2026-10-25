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
            pricing.estimate_cost("claude-haiku-3-5", usage), 0.80)
        self.assertAlmostEqual(
            pricing.estimate_cost("claude-haiku-4-5", usage), 1.00)

    def test_current_models_use_current_published_rates(self):
        one = {"input": 1_000_000, "output": 0, "cache_write": 0,
               "cache_read": 0}
        for model, want in (("claude-opus-4-5", 5.0),
                            ("claude-opus-4-5-20251101", 5.0),
                            ("claude-opus-4-8", 5.0), ("claude-opus-5", 5.0),
                            ("claude-opus-5-5", 4.0),
                            ("claude-opus-4-1-20250805", 15.0),
                            ("claude-opus-4-20250514", 15.0),
                            ("claude-3-opus-20240229", 15.0),
                            ("claude-sonnet-4-5", 3.0),
                            ("claude-sonnet-5", 2.0),
                            ("claude-3-5-haiku-20241022", 0.80),
                            ("claude-haiku-4-5-20251001", 1.0)):
            self.assertAlmostEqual(pricing.estimate_cost(model, one), want,
                                   msg=model)
        out = {"input": 0, "output": 1_000_000, "cache_write": 0,
               "cache_read": 0}
        self.assertAlmostEqual(
            pricing.estimate_cost("claude-opus-4-5", out), 25.0)
        self.assertAlmostEqual(
            pricing.estimate_cost("claude-haiku-4-5", out), 5.0)
        self.assertEqual(pricing.match("claude-opus-4-5", None)[0], "opus-4-5")

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
            # a plain rates dict still works for estimate_cost
            self.assertAlmostEqual(
                pricing.estimate_cost("claude-sonnet-4-5", usage, table.rates),
                10.00)
        finally:
            os.unlink(path)

    def test_load_table_none_returns_defaults(self):
        self.assertEqual(pricing.load_table(None, environ={}).rates,
                         pricing.DEFAULT_PRICING)


if __name__ == "__main__":
    unittest.main()
