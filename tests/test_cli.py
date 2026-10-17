"""CLI-level tests: secrets redaction, clean errors, quick-start commands."""

import _isolate  # noqa: F401  (isolate ~/.shortleash before importing)

import contextlib
import io
import json
import os
import sys
import tempfile
import threading
import unittest
from contextlib import redirect_stderr, redirect_stdout

from shortleash import cli as cli_mod
from shortleash import receipt as receipt_mod

PY = sys.executable
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SIM = os.path.join(REPO, "sim", "runaway.py")


@contextlib.contextmanager
def capture_stderr_fd():
    """Capture os-level stderr (fd 2), so spawned children are captured too.

    redirect_stderr alone breaks subprocess.Popen(stderr=sys.stderr)
    because a StringIO has no fileno — this dup2's fd 2 into a pipe.
    sys.stderr is rebound to that same pipe for the duration: under
    pytest it is a capture object that bypasses fd 2 entirely, and
    without this the Python-level writes would never reach the pipe.
    A reader thread drains the pipe so a chatty child can never fill
    the (small, on Windows) pipe buffer and deadlock.
    Yields a list of byte chunks; join and decode to inspect.
    """
    r, w = os.pipe()
    saved_fd = os.dup(2)
    os.dup2(w, 2)
    os.close(w)
    saved_stream = sys.stderr
    stream = open(os.dup(2), "w", encoding="utf-8", errors="replace",
                  closefd=True)
    chunks: list[bytes] = []

    def _drain() -> None:
        while True:
            data = os.read(r, 65536)
            if not data:
                break
            chunks.append(data)

    reader = threading.Thread(target=_drain, daemon=True)
    reader.start()
    sys.stderr = stream
    try:
        yield chunks
    finally:
        sys.stderr = saved_stream
        try:
            stream.close()
        except OSError:
            pass
        os.dup2(saved_fd, 2)
        os.close(saved_fd)
        reader.join(timeout=10)
        os.close(r)


def _base_kwargs() -> dict:
    return dict(started_at=1.0, ended_at=2.0, breached=False,
                breach_reason=None, cost_usd=0.01, cost_estimated=True,
                usage={"input": 10}, num_turns=1, spawns=0, exit_code=0,
                model="m", session_id="s")


class TestRedactArgv(unittest.TestCase):
    def test_flag_value_pair(self):
        self.assertEqual(
            receipt_mod.redact_argv(["claude", "--api-key", "sk-secret123"]),
            ["claude", "--api-key", "***"])

    def test_flag_equals(self):
        self.assertEqual(
            receipt_mod.redact_argv(["claude", "--api-key=sk-secret123"]),
            ["claude", "--api-key=***"])

    def test_env_style(self):
        self.assertEqual(
            receipt_mod.redact_argv(["claude", "api_key=sk-secret123"]),
            ["claude", "api_key=***"])

    def test_case_insensitive_names(self):
        self.assertEqual(
            receipt_mod.redact_argv(["x", "--Auth-TOKEN", "abc"]),
            ["x", "--Auth-TOKEN", "***"])
        self.assertEqual(
            receipt_mod.redact_argv(["x", "DB_PASSWORD=hunter2"]),
            ["x", "DB_PASSWORD=***"])

    def test_non_secret_args_untouched(self):
        argv = ["claude", "-p", "do thing", "--budget", "2.00"]
        self.assertEqual(receipt_mod.redact_argv(argv), argv)

    def test_does_not_mutate_input(self):
        argv = ["claude", "--api-key", "sk-secret123"]
        receipt_mod.redact_argv(argv)
        self.assertEqual(argv[2], "sk-secret123")

    def test_receipt_never_contains_secret(self):
        rec = receipt_mod.build_receipt(
            command=["claude", "--api-key", "sk-secret123",
                     "--token=tok-abc", "passwd=hunter2"],
            **_base_kwargs())
        blob = json.dumps(rec)
        self.assertNotIn("sk-secret123", blob)
        self.assertNotIn("tok-abc", blob)
        self.assertNotIn("hunter2", blob)
        self.assertIn("***", blob)
        self.assertIn("--api-key", blob)  # flag name kept, value redacted


class TestSecretsEndToEnd(unittest.TestCase):
    def test_secret_never_in_stderr_or_receipt(self):
        with tempfile.NamedTemporaryFile("w", suffix=".json",
                                         delete=False) as f:
            receipt_path = f.name
        tmpdir = tempfile.mkdtemp()
        # The sim child will reject the unknown flag, but the guard still
        # writes a receipt — both must be secret-free.
        # Use a child that exits cleanly without echoing its argv (the sim's
        # argparse usage-error would itself print the secret — that's the
        # child's output, not ours, and out of scope for this test).
        child = [PY, "-c", "pass", "--api-key", "sk-secret123"]
        with capture_stderr_fd() as chunks:
            code = cli_mod.main(
                ["run", "--budget", "10", "--no-anomaly",
                 "--history-path", os.path.join(tmpdir, "h.jsonl"),
                 "--receipt", receipt_path, "--"] + child)
        stderr_text = b"".join(chunks).decode("utf-8", "replace")
        self.assertNotIn("sk-secret123", stderr_text,
                         "secret leaked into stderr")
        with open(receipt_path, encoding="utf-8") as f:
            blob = f.read()
        self.assertNotIn("sk-secret123", blob,
                         "secret leaked into receipt JSON")
        rec = json.loads(blob)
        self.assertIn("***", json.dumps(rec["command"]))
        os.unlink(receipt_path)


class TestCleanErrors(unittest.TestCase):
    def test_nonexistent_command(self):
        with capture_stderr_fd() as chunks:
            code = cli_mod.main(["run", "--no-anomaly", "--",
                                 "no-such-binary-xyz-123"])
        stderr_text = b"".join(chunks).decode("utf-8", "replace")
        self.assertEqual(code, 2)
        self.assertIn("not found", stderr_text)
        self.assertNotIn("Traceback", stderr_text)

    def test_malformed_pricing_json(self):
        with tempfile.NamedTemporaryFile("w", suffix=".json",
                                         delete=False) as f:
            f.write("{not valid json")
            bad = f.name
        err = io.StringIO()
        try:
            with redirect_stderr(err):
                code = cli_mod.main(["run", "--no-anomaly",
                                     "--pricing", bad, "--",
                                     PY, SIM, "--well-behaved"])
        finally:
            os.unlink(bad)
        self.assertEqual(code, 2)
        self.assertIn("pricing", err.getvalue())
        self.assertNotIn("Traceback", err.getvalue())

    def test_missing_pricing_file(self):
        err = io.StringIO()
        with redirect_stderr(err):
            code = cli_mod.main(["run", "--no-anomaly",
                                 "--pricing", "/nope/pricing.json", "--",
                                 PY, SIM, "--well-behaved"])
        self.assertEqual(code, 2)
        self.assertNotIn("Traceback", err.getvalue())


class TestQuickStartCommands(unittest.TestCase):
    """Every command used in the README quick-start must exist."""

    def _prompt(self, text: str) -> str:
        p = os.path.join(tempfile.mkdtemp(), "p.txt")
        with open(p, "w", encoding="utf-8") as f:
            f.write(text)
        return p

    def test_run_help(self):
        with redirect_stdout(io.StringIO()):
            with self.assertRaises(SystemExit) as cm:
                cli_mod.main(["run", "--help"])
        self.assertEqual(cm.exception.code, 0)

    def test_cache_guide(self):
        with redirect_stdout(io.StringIO()):
            self.assertEqual(cli_mod.main(["cache", "guide"]), 0)

    def test_cache_check(self):
        p = self._prompt("You are {{role}}. Go.")
        with redirect_stdout(io.StringIO()):
            self.assertEqual(cli_mod.main(["cache", "check", "-i", p]), 0)

    def test_non_ascii_output_survives_cp1252_console(self):
        # Windows consoles often run cp1252 with errors="strict"; the ✓ in
        # the verdict used to raise UnicodeEncodeError. main() hardens the
        # streams so the character is replaced instead.
        p = self._prompt("Static prompt, fully cacheable.")
        raw = io.BytesIO()
        strict = io.TextIOWrapper(raw, encoding="cp1252", errors="strict")
        with redirect_stdout(strict):
            code = cli_mod.main(["cache", "check", "-i", p])
            strict.flush()
        self.assertEqual(code, 0)
        text = raw.getvalue().decode("cp1252")
        self.assertIn("fully cacheable already", text)
        self.assertNotIn("✓", text)  # replaced, not crashed

    def test_cache_make_all_providers(self):
        p = self._prompt("Rules. Q: {{q}}")
        for prov in ("all", "anthropic", "openai", "gemini", "deepseek"):
            with redirect_stdout(io.StringIO()):
                self.assertEqual(
                    cli_mod.main(["cache", "make", "-i", p,
                                  "--provider", prov]), 0)


if __name__ == "__main__":
    unittest.main()
