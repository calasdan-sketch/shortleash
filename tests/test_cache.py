"""Cache optimizer tests."""

import io
import os
import tempfile
import unittest
from contextlib import redirect_stdout

from shortleash import cacheopt
from shortleash import cli as cli_mod


class TestAnalyze(unittest.TestCase):
    def test_placeholders(self):
        a = cacheopt.analyze("Hello {{name}}, you are {{role}}.")
        self.assertEqual(a["placeholders"], ["name", "role"])
        self.assertEqual(a["n_placeholders"], 2)

    def test_no_placeholders(self):
        a = cacheopt.analyze("Static prompt.")
        self.assertEqual(a["placeholders"], [])
        self.assertEqual(a["static_ratio"], 1.0)

    def test_dedupes(self):
        a = cacheopt.analyze("{{x}} and {{x}}")
        self.assertEqual(a["placeholders"], ["x"])


class TestRestructure(unittest.TestCase):
    def test_static_first_vars_last(self):
        text = "You are a {{role}}. Follow these rules:\n1. Be kind.\nUser: {{input}}"
        out, notes = cacheopt.restructure(text)
        # every placeholder appears exactly once, after the static block
        for var in ("{{role}}", "{{input}}"):
            self.assertEqual(out.count(var), 1)
        static_end = out.find("---")
        self.assertLess(out.find("You are a"), static_end)
        self.assertGreater(out.find("{{role}}"), static_end)

    def test_static_preserved(self):
        text = "Rules:\n1. Be kind.\n2. Be brief.\nQ: {{q}}"
        out, _ = cacheopt.restructure(text)
        for line in ("Rules:", "1. Be kind.", "2. Be brief."):
            self.assertIn(line, out)

    def test_no_vars_passthrough(self):
        out, notes = cacheopt.restructure("Static.")
        self.assertEqual(out, "Static.")
        self.assertTrue(any("fully cacheable" in n for n in notes))

    def test_notes_warn_review(self):
        _, notes = cacheopt.restructure("Hi {{name}}")
        self.assertTrue(any("Review" in n for n in notes))

    def test_placeholders_survive_exactly_once(self):
        # Regression: every {{var}} in the input must appear exactly once
        # in the restructured output, with static text first. Placeholders
        # are relocated below the breakpoint, never deleted.
        cases = [
            "Hello {{name}}, you are {{role}}. Follow: {{name}} again.",
            "You are a {{role}}. Follow these rules:\n1. Be kind.\nUser: {{input}}",
            "Pre {{a}} mid {{b}} post {{c}} end",
        ]
        for text in cases:
            out, _ = cacheopt.restructure(text)
            names = cacheopt.find_placeholders(text)
            self.assertTrue(names, f"no placeholders found in {text!r}")
            static_end = out.find("---")
            self.assertGreater(static_end, 0)
            for name in names:
                token = "{{" + name + "}}"
                self.assertEqual(out.count(token), 1,
                                 f"{token} should appear exactly once "
                                 f"for input {text!r}")
                self.assertGreater(out.find(token), static_end,
                                   f"{token} should sit below the breakpoint")


class TestUnicodePlaceholders(unittest.TestCase):
    def test_find_unicode(self):
        self.assertEqual(cacheopt.find_placeholders("Bonjour {{naïve}}!"),
                         ["naïve"])

    def test_restructure_unicode(self):
        out, _ = cacheopt.restructure("Bonjour {{naïve}}, rôle: {{rôle}}.")
        for token in ("{{naïve}}", "{{rôle}}"):
            self.assertEqual(out.count(token), 1)


class TestProviders(unittest.TestCase):
    def test_guide_has_verified_links(self):
        g = cacheopt.guide_text()
        for _, url, _ in cacheopt.CACHE_DOCS:
            self.assertIn(url, g)
            self.assertTrue(url.startswith("https://"))

    def test_snippets(self):
        self.assertIn("cache_control", cacheopt.provider_snippet("anthropic"))
        self.assertIn("automatic", cacheopt.provider_snippet("openai").lower())
        self.assertIn("cached_content", cacheopt.provider_snippet("gemini"))
        with self.assertRaises(ValueError):
            cacheopt.provider_snippet("nope")


class TestCLI(unittest.TestCase):
    def _prompt(self, text: str) -> str:
        tmp = tempfile.mkdtemp()
        p = os.path.join(tmp, "p.txt")
        with open(p, "w", encoding="utf-8") as f:
            f.write(text)
        return p

    def test_guide(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = cli_mod.main(["cache", "guide"])
        self.assertEqual(code, 0)
        self.assertIn("anthropic.com", buf.getvalue())

    def test_check(self):
        p = self._prompt("You are {{role}}. Go.")
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = cli_mod.main(["cache", "check", "-i", p])
        self.assertEqual(code, 0)
        self.assertIn("role", buf.getvalue())

    def test_make_stdout(self):
        p = self._prompt("You are a {{role}}.\nUser: {{input}}")
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = cli_mod.main(["cache", "make", "-i", p,
                                 "--provider", "openai"])
        out = buf.getvalue()
        self.assertEqual(code, 0)
        self.assertIn("OPTIMIZED PROMPT", out)
        self.assertIn("{{role}}", out)

    def test_make_file(self):
        p = self._prompt("Rules. Q: {{q}}")
        outp = p + ".opt.txt"
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = cli_mod.main(["cache", "make", "-i", p, "-o", outp])
        self.assertEqual(code, 0)
        with open(outp, encoding="utf-8") as f:
            content = f.read()
        self.assertIn("{{q}}", content)
        self.assertIn("Rules.", content)

    def test_make_missing_file(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = cli_mod.main(["cache", "make", "-i", "/nope/x.txt"])
        self.assertEqual(code, 2)


if __name__ == "__main__":
    unittest.main()
