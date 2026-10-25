"""Round 10: regression tests.

R10-1 The release workflow crashed on the dynamic version (see test_release_gate).
R10-2 argv redaction mangled prose and `--author`, and missed `-uuser:pass`,
      URL user:password@host and `password:value`.
R10-3 Model-price notes (claude-3-haiku, 1-hour cache writes) are documented.
"""

import _isolate  # noqa: F401

import os
import unittest

from shortleash import pricing
from shortleash.receipt import redact_argv

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class TestNoOverRedaction(unittest.TestCase):
    def test_the_reported_command_is_unchanged(self):
        argv = ["claude", "-p",
                "write basic functionality for the disk-usage-report",
                "--author", "bob"]
        self.assertEqual(redact_argv(argv), argv)

    def test_author_flags_are_not_secrets(self):
        for argv in (["--author", "bob"], ["--author=bob"], ["git", "-c",
                     "user.author=bob"], ["--authors", "a,b"]):
            self.assertEqual(redact_argv(argv), argv)

    def test_sk_inside_a_word_is_left_alone(self):
        for word in ("disk-usage-report", "task-abcdefghijkl",
                     "risk-assessment-notes", "desk-organizer-project"):
            self.assertEqual(redact_argv([word]), [word])

    def test_basic_and_bearer_in_prose_are_left_alone(self):
        for text in ("explain basic functionality", "a basic understanding of it",
                     "the bearer of bad news", "bearer bonds are old"):
            self.assertEqual(redact_argv(["-p", text]), ["-p", text], text)

    def test_embedded_words_do_not_trigger_bearer(self):
        self.assertEqual(redact_argv(["unbearer abcdefghij"]),
                         ["unbearer abcdefghij"])


class TestStillRedacted(unittest.TestCase):
    def test_real_keys(self):
        self.assertEqual(redact_argv(["sk-ant-api03-abcdefghijkl"]), ["***"])
        self.assertEqual(redact_argv(["use", "sk-abcdefghij", "now"]),
                         ["use", "***", "now"])
        self.assertEqual(redact_argv(["key=sk-abcdefghijkl"]), ["key=***"])

    def test_bearer_and_basic_credentials(self):
        self.assertEqual(redact_argv(["Bearer abcdefghij123"]), ["***"])
        self.assertEqual(redact_argv(["Basic dXNlcjpwYXNz"]), ["***"])
        self.assertEqual(redact_argv(["-H", "Authorization: Basic dXNlcjpwYXNz"]),
                         ["-H", "Authorization: ***"])

    def test_auth_as_a_word(self):
        for flag in ("--auth", "--oauth", "--authorization", "--x-auth",
                     "--authentication", "--auth_header"):
            self.assertEqual(redact_argv([flag, "secretvalue"]),
                             [flag, "***"], flag)

    def test_other_names_still_match(self):
        self.assertEqual(redact_argv(["--api-key", "v", "--db-password=p",
                                      "TOKEN=t"]),
                         ["--api-key", "***", "--db-password=***", "TOKEN=***"])


class TestNewlyRedacted(unittest.TestCase):
    def test_glued_short_user_flag(self):
        self.assertEqual(redact_argv(["curl", "-uuser:pass", "x"]),
                         ["curl", "-u***", "x"])
        self.assertEqual(redact_argv(["-Uadmin:s3cret"]), ["-U***"])

    def test_glued_flag_without_a_colon_is_left_alone(self):
        self.assertEqual(redact_argv(["-unbuffered"]), ["-unbuffered"])

    def test_url_userinfo(self):
        self.assertEqual(redact_argv(["https://u:p@h/x"]), ["https://***@h/x"])
        self.assertEqual(redact_argv(["git", "clone", "ssh://git:pw@host:22/r"]),
                         ["git", "clone", "ssh://***@host:22/r"])

    def test_url_without_userinfo_is_unchanged(self):
        for url in ("https://example.com/a:b", "http://h:8080/x@y",
                    "https://example.com/?next=a:b@c"):
            self.assertEqual(redact_argv([url]), [url])

    def test_colon_form(self):
        self.assertEqual(redact_argv(["password:hunter2"]), ["password:***"])
        self.assertEqual(redact_argv(["api-key:abc", "Token:xyz"]),
                         ["api-key:***", "Token:***"])

    def test_colon_form_of_a_harmless_name_is_unchanged(self):
        self.assertEqual(redact_argv(["note:hello", "author:bob", "http://h"]),
                         ["note:hello", "author:bob", "http://h"])

    def test_original_argv_is_not_modified(self):
        argv = ["-uuser:pass", "password:x"]
        redact_argv(argv)
        self.assertEqual(argv, ["-uuser:pass", "password:x"])


class TestPriceNotes(unittest.TestCase):
    def test_claude_3_haiku_falls_back_to_the_haiku_family(self):
        key, rate = pricing.match("claude-3-haiku-20240307")
        self.assertEqual(key, "haiku")
        self.assertEqual(rate, (1.00, 5.00))

    def test_a_user_entry_prices_claude_3_haiku(self):
        table = pricing.PricingTable(
            rates={**pricing.DEFAULT_PRICING, "3-haiku": (0.25, 1.25)},
            overrides={"3-haiku": (0.25, 1.25)})
        self.assertEqual(pricing.match("claude-3-haiku-20240307", table)[1],
                         (0.25, 1.25))
        self.assertEqual(pricing.match("claude-3-5-haiku-latest", table)[1],
                         (0.80, 4.00))

    def test_cache_write_is_priced_at_the_five_minute_rate(self):
        cost = pricing.estimate_cost(
            "claude-sonnet-4", {"cache_write": 1_000_000})
        self.assertAlmostEqual(cost, 3.00 * 1.25)

    def test_readme_documents_the_limits(self):
        with open(os.path.join(ROOT, "README.md"), encoding="utf-8") as f:
            text = f.read()
        for needle in ("8 million characters", "1 hour", "claude-3-haiku",
                       "pieces"):
            self.assertIn(needle, text)


if __name__ == "__main__":
    unittest.main()
