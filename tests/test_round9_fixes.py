"""Round 9: regression tests.

R9-1 `--fleet` with a stderr that closed early leaked the claim and never
     started the agent.
R9-2 One agent output line of hundreds of MB was buffered whole.
R9-3 argv redaction missed Authorization headers, --auth/--bearer/
     --credential/--cookie flags and bare sk-... arguments; receipts were
     world-readable.
R9-4 Pricing table matches Anthropic's published rates.
"""

import _isolate  # noqa: F401  (isolate ~/.shortleash before importing)

import io
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout

from shortleash import guard as guard_mod
from shortleash import pricing
from shortleash import receipt as receipt_mod
from shortleash.guard import Guard

PY = sys.executable


def _tmp() -> str:
    d = tempfile.mkdtemp(prefix="sl9-")
    return d


class TestFleetWithClosedStderr(unittest.TestCase):
    @unittest.skipIf(os.name == "nt", "needs a pipe whose reader is gone")
    def test_agent_still_runs_and_claim_is_released(self):
        tmp = _tmp()
        self.addCleanup(shutil.rmtree, tmp, True)
        marker = os.path.join(tmp, "ran")
        r, w = os.pipe()
        os.close(r)                      # nobody reads stderr: EPIPE at once
        env = dict(os.environ, SHORTLEASH_HOME=tmp)
        env.pop("PYTHONUNBUFFERED", None)
        repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        env["PYTHONPATH"] = repo + os.pathsep + env.get("PYTHONPATH", "")
        try:
            proc = subprocess.run(
                [PY, "-m", "shortleash", "run", "--fleet", "--budget", "3",
                 "--claim", tmp, "--", PY, "-c",
                 "open(%r, 'w').write('x')" % marker],
                stdout=subprocess.DEVNULL, stderr=w, env=env, cwd=tmp,
                timeout=60)
        finally:
            os.close(w)
        self.assertTrue(os.path.exists(marker), "the agent never started")
        lst = subprocess.run(
            [PY, "-m", "shortleash", "fleet", "list"], capture_output=True,
            text=True, env=env, cwd=tmp, timeout=60)
        self.assertIn("no active fleet claims", lst.stdout, lst.stdout)


class TestLongLine(unittest.TestCase):
    def test_overlong_line_is_skipped_and_later_events_still_count(self):
        tmp = _tmp()
        self.addCleanup(shutil.rmtree, tmp, True)
        code = (
            "import json, sys\n"
            "print(json.dumps({'type': 'system', 'subtype': 'init', "
            "'model': 'claude-haiku-4-5', 'session_id': 's'}), flush=True)\n"
            "sys.stdout.write('x' * 300000 + chr(10)); sys.stdout.flush()\n"
            "print(json.dumps({'type': 'assistant', 'message': {'id': 'm', "
            "'content': [], 'usage': {'input_tokens': 1000000}}}), flush=True)\n")
        old = guard_mod.MAX_LINE_CHARS
        guard_mod.MAX_LINE_CHARS = 100_000
        self.addCleanup(setattr, guard_mod, "MAX_LINE_CHARS", old)
        g = Guard(budget=1000.0, quiet=True)
        g.receipt_path = os.path.join(tmp, "r.json")
        g.history_path = os.path.join(tmp, "h.jsonl")
        with redirect_stderr(io.StringIO()), redirect_stdout(io.StringIO()):
            g.run([PY, "-c", code])
        with open(g.receipt_path, encoding="utf-8") as f:
            rec = json.load(f)
        self.assertAlmostEqual(rec["cost_usd"], 1.0)   # haiku 4.5: $1 / 1M in
        self.assertTrue(any("longer than" in w for w in g.warnings))

    def test_normal_lines_around_the_cap_boundary_are_whole(self):
        # a line longer than one read chunk but under the cap is parsed whole
        tmp = _tmp()
        self.addCleanup(shutil.rmtree, tmp, True)
        pad = "y" * 200_000
        code = (
            "import json\n"
            "print(json.dumps({'type': 'system', 'subtype': 'init', "
            "'model': 'claude-haiku-4-5', 'session_id': 's', 'pad': %r}), "
            "flush=True)\n"
            "print(json.dumps({'type': 'assistant', 'message': {'id': 'm', "
            "'content': [], 'usage': {'input_tokens': 1000000}}}), flush=True)\n"
            % pad)
        script = os.path.join(tmp, "child.py")   # too big for argv
        with open(script, "w", encoding="utf-8") as f:
            f.write(code)
        g = Guard(budget=1000.0, quiet=True)
        g.receipt_path = os.path.join(tmp, "r.json")
        g.history_path = os.path.join(tmp, "h.jsonl")
        with redirect_stderr(io.StringIO()), redirect_stdout(io.StringIO()):
            g.run([PY, script])
        with open(g.receipt_path, encoding="utf-8") as f:
            rec = json.load(f)
        self.assertEqual(rec["model"], "claude-haiku-4-5")
        self.assertEqual(g.warnings, [])


class TestRedaction(unittest.TestCase):
    def red(self, *argv):
        return receipt_mod.redact_argv(list(argv))

    def test_authorization_header_forms(self):
        self.assertEqual(
            self.red("curl", "--header", "Authorization: Bearer abc123def456"),
            ["curl", "--header", "Authorization: ***"])
        self.assertEqual(
            self.red("curl", "-H", "Cookie: sid=zzz"),
            ["curl", "-H", "Cookie: ***"])
        self.assertEqual(
            self.red("curl", "--header=Authorization: Basic dXNlcjpwYXNz"),
            ["curl", "--header=Authorization: ***"])
        self.assertEqual(self.red("curl", "-H", "Accept: json"),
                         ["curl", "-H", "Accept: json"])

    def test_auth_bearer_credential_cookie_flags(self):
        for flag in ("--auth", "--bearer", "--credential", "--cookie"):
            self.assertEqual(self.red("x", flag, "s3cret"), ["x", flag, "***"])
            self.assertEqual(self.red("x", f"{flag}=s3cret"),
                             ["x", f"{flag}=***"])
        self.assertEqual(self.red("curl", "-u", "me:pw"),
                         ["curl", "-u", "***"])

    def test_bare_secret_looking_arguments(self):
        self.assertEqual(self.red("tool", "sk-ant-api03-abcdefgh12345"),
                         ["tool", "***"])
        self.assertEqual(self.red("tool", "--url=https://h/?k=sk-abcdefgh1234"),
                         ["tool", "--url=https://h/?k=***"])
        self.assertEqual(self.red("tool", "ghp_" + "a" * 30), ["tool", "***"])
        self.assertEqual(self.red("tool", "hello", "--flag", "value"),
                         ["tool", "hello", "--flag", "value"])

    def test_receipt_file_is_private(self):
        tmp = _tmp()
        self.addCleanup(shutil.rmtree, tmp, True)
        path = os.path.join(tmp, "r.json")
        with open(path, "w") as f:
            f.write("{}")
        os.chmod(path, 0o644)           # pre-existing and world-readable
        receipt_mod.write_receipt({"a": 1}, path)
        if os.name != "nt":
            self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
        fresh = os.path.join(tmp, "new.json")
        receipt_mod.write_receipt({"a": 1}, fresh)
        if os.name != "nt":
            self.assertEqual(stat.S_IMODE(os.stat(fresh).st_mode), 0o600)
        with open(fresh, encoding="utf-8") as f:
            self.assertEqual(json.load(f), {"a": 1})


class TestPricingAsOf(unittest.TestCase):
    def test_as_of_is_updated_and_readme_example_matches(self):
        self.assertEqual(pricing.PRICING_AS_OF, "2026-10-24")
        repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(repo, "README.md"), encoding="utf-8") as f:
            readme = f.read()
        self.assertIn('"opus": [5.0, 25.0], "haiku": [1.0, 5.0]', readme)
        self.assertNotIn("2026-10-16", readme)


if __name__ == "__main__":
    unittest.main()
