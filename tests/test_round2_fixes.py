"""Round 2 panel fixes: regression tests.

- FIX 1 (security): malformed telemetry fails CLOSED — a child line with
  non-numeric usage ({"input_tokens": "abc"}) must trip the run, kill the
  child, and write a receipt. Pre-fix, the ValueError killed the observer
  thread and the supervisor blocked on proc.wait() forever.
- FIX 2 (DevEx): negative caps (--budget/--max-spawns/--max-tokens/--timeout)
  are rejected with exit code 2 and a clean stderr message.
- FIX 3 (launch blocker): version single source of truth —
  shortleash.__version__ == pyproject == receipt version. Plus --version.
"""

import _isolate  # noqa: F401  (isolate ~/.shortleash before importing)

import io
import json
import os
import sys
import tempfile
import tomllib
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

import shortleash
from shortleash import cli as cli_mod
from shortleash import receipt as receipt_mod
from shortleash.guard import Guard, BREACH_EXIT_CODE

PY = sys.executable
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Panel PoC: one garbage stream-json line, then the child blocks.
GARBAGE_CHILD = (
    "import json, sys, time; "
    "sys.stdout.write(json.dumps({'type': 'assistant', 'message': "
    "{'content': [], 'usage': {'input_tokens': 'abc', "
    "'output_tokens': 1}}}) + chr(10)); "
    "sys.stdout.flush(); "
    "time.sleep(120)"
)


def _isolated_receipt():
    f = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False)
    f.close()
    return f.name


class TestMalformedTelemetryFailClosed(unittest.TestCase):
    """One bad telemetry line must trip/kill/receipt, never hang."""

    def test_garbage_line_trips_run(self):
        receipt_path = _isolated_receipt()
        tmpdir = tempfile.mkdtemp()
        g = Guard(
            budget=1000.0,
            max_tokens=10 ** 12,
            timeout=10.0,          # backstop only: the malformed trip fires in ms
            grace_period=0.2,
            receipt_path=receipt_path,
            quiet=True,
            anomaly=None,          # no history lookup in this test
            history_path=os.path.join(tmpdir, "h.jsonl"),
        )
        try:
            code = g.run([PY, "-c", GARBAGE_CHILD])
        finally:
            # Belt and suspenders: even if the bug regressed, the child is
            # reaped here so it can't outlive the suite.
            try:
                if g.proc is not None:
                    g._kill_tree()
            except Exception:
                pass
        self.assertEqual(code, BREACH_EXIT_CODE,
                         "malformed telemetry must trip with exit 124")
        with open(receipt_path, encoding="utf-8") as f:
            rec = json.load(f)
        self.assertTrue(rec["breached"], "receipt must record the trip")
        self.assertIn("malformed", (rec["breach_reason"] or "").lower(),
                      "receipt must note malformed telemetry")
        self.assertEqual(rec["exit_code"], BREACH_EXIT_CODE)


class TestNegativeCapsRejected(unittest.TestCase):
    def _expect_rejection(self, flag: str, value: str):
        err = io.StringIO()
        with redirect_stderr(err):
            with self.assertRaises(SystemExit) as cm:
                cli_mod.main(["run", flag, value, "--", "true"])
        self.assertEqual(cm.exception.code, 2,
                         f"{flag} {value}: expected exit code 2")
        stderr = err.getvalue()
        self.assertIn(flag, stderr)
        self.assertIn(">= 0", stderr)

    def test_negative_budget(self):
        self._expect_rejection("--budget", "-5")

    def test_negative_max_spawns(self):
        self._expect_rejection("--max-spawns", "-1")

    def test_negative_max_tokens(self):
        self._expect_rejection("--max-tokens", "-3")

    def test_negative_timeout(self):
        self._expect_rejection("--timeout", "-0.5")

    def test_zero_caps_still_allowed(self):
        """Zero is a valid (draconian) cap — validation must not fire."""
        seen = {}

        class StubGuard:
            def __init__(self, **kwargs):
                seen.update(kwargs)

            def run(self, cmd):
                return 0

        with mock.patch.object(cli_mod.guard_mod, "Guard", StubGuard):
            err = io.StringIO()
            with redirect_stderr(err):
                code = cli_mod.main(
                    ["run", "--budget", "0", "--max-spawns", "0",
                     "--max-tokens", "0", "--timeout", "0",
                     "--quiet", "--no-anomaly", "--", "true"])
        self.assertEqual(code, 0)
        self.assertEqual(seen["budget"], 0)
        self.assertEqual(seen["max_spawns"], 0)
        self.assertEqual(seen["max_tokens"], 0)
        self.assertEqual(seen["timeout"], 0)


class TestVersionSingleSource(unittest.TestCase):
    def _pyproject(self) -> dict:
        with open(os.path.join(REPO, "pyproject.toml"), "rb") as f:
            return tomllib.load(f)

    def test_pyproject_reads_version_dynamically(self):
        pp = self._pyproject()
        self.assertIn("version", pp["project"].get("dynamic", []),
                      "pyproject must declare version dynamically")
        attr = (pp["tool"]["setuptools"]["dynamic"]["version"]["attr"])
        self.assertEqual(attr, "shortleash.__version__")

    def test_version_resolves_to_package_version(self):
        pp = self._pyproject()
        attr = pp["tool"]["setuptools"]["dynamic"]["version"]["attr"]
        mod_name, attr_name = attr.rsplit(".", 1)
        self.assertEqual(mod_name, "shortleash")
        self.assertEqual(getattr(shortleash, attr_name),
                         shortleash.__version__)

    def test_receipt_version_matches_package(self):
        rec = receipt_mod.build_receipt(
            command=["true"], started_at=1.0, ended_at=2.0,
            breached=False, breach_reason=None, cost_usd=0.0,
            cost_estimated=True, usage={}, num_turns=0, spawns=0,
            exit_code=0, model="", session_id="")
        self.assertEqual(rec["version"], shortleash.__version__)

    def test_cli_version_flag(self):
        out = io.StringIO()
        with redirect_stdout(out):
            with self.assertRaises(SystemExit) as cm:
                cli_mod.main(["--version"])
        self.assertEqual(cm.exception.code, 0)
        self.assertIn(shortleash.__version__, out.getvalue())


if __name__ == "__main__":
    unittest.main()
