"""Round 6 (second verification panel): regression tests, one class per finding.

L-1 `cache check|make -i FILE` died with a traceback on a prompt file that is
    not UTF-8 (PowerShell 5's `>` writes UTF-16) and on `-o` into a missing
    directory; both are now one `error:` line and exit 2, and UTF-8 / UTF-16 /
    UTF-32 files that start with a byte-order mark are simply read.
L-2 A command's own exit code 124 / 125 / 126 / 143 passes through and looks
    like the leash's verdict of that number; the README now says that only the
    receipt tells them apart, and these tests pin that the receipt does.
L-3 `--output-format=json` (equals form) was not recognised as the user's own
    choice: a second `--output-format stream-json --verbose` was appended.
L-4 `--anomaly --min-history 0` was a StatisticsError traceback (exit 1) at
    the start of the run: the median of no runs.
L-5 A `usage` block with fields but none the leash reads (`prompt_toks`)
    counted as zero tokens: `--max-tokens 100` and 5,000,000 reported tokens
    ended "finished within guardrails", exit 0.
L-6 The README said the agent's stdout "passes through untouched"; the echo is
    a best-effort copy (a carriage return becomes a newline, bytes that are
    not UTF-8 become U+FFFD, a burst a reader cannot keep up with loses lines).
    The README now says so, and these tests pin what it says.

Real-subprocess tests remove PYTHONUNBUFFERED from the child's environment
(it hides buffered-flush failures).
"""

import _isolate  # noqa: F401  (isolate ~/.shortleash before importing)

import codecs
import errno
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

from shortleash import cacheopt
from shortleash import cli as cli_mod
from shortleash import events
from shortleash import history as history_mod
from shortleash import receipt as receipt_mod
from shortleash.guard import Guard, _EchoPump

PY = sys.executable
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _tmp() -> str:
    return tempfile.mkdtemp(prefix="sl6-")


def _child_env() -> dict:
    env = dict(os.environ)
    env.pop("PYTHONUNBUFFERED", None)
    return env


def _cli(*argv):
    """Run the CLI in this process: (exit code, stdout, stderr)."""
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        rc = cli_mod.main(list(argv))
    return rc, out.getvalue(), err.getvalue()


def _run_guard(guard: Guard, argv: list):
    """Run the leash in this process: (exit code, receipt dict)."""
    tmp = _tmp()
    guard.receipt_path = os.path.join(tmp, "r.json")
    guard.history_path = os.path.join(tmp, "h.jsonl")
    with redirect_stderr(io.StringIO()), redirect_stdout(io.StringIO()):
        code = guard.run(argv)
    with open(guard.receipt_path, encoding="utf-8") as f:
        rec = json.load(f)
    shutil.rmtree(tmp, ignore_errors=True)
    return code, rec


def _readme() -> str:
    with open(os.path.join(REPO, "README.md"), encoding="utf-8") as f:
        return f.read()


# ---------------------------------------------------------------------
# L-1

PROMPT = "You are a {{role}}.\nRules: be brief.\nQ: {{q}}\n"


class TestCachePromptFiles(unittest.TestCase):
    """The prompt file may be whatever a Windows editor or shell wrote."""

    def setUp(self):
        self.tmp = _tmp()
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def _file(self, data: bytes, name: str = "p.txt") -> str:
        path = os.path.join(self.tmp, name)
        with open(path, "wb") as f:
            f.write(data)
        return path

    def _one_error_line(self, err: str, path: str) -> str:
        lines = err.splitlines()
        self.assertEqual(len(lines), 1, err)
        self.assertTrue(lines[0].startswith("error: "), err)
        self.assertIn(path, lines[0])
        self.assertNotIn("Traceback", err)
        return lines[0]

    def _assert_read_as(self, data: bytes, text: str) -> None:
        path = self._file(data)
        want = cacheopt.analyze(text)
        rc, out, err = _cli("cache", "check", "-i", path)
        self.assertEqual((rc, err), (0, ""))
        self.assertIn("placeholders : " + ", ".join(want["placeholders"]), out)
        # counted without the byte-order mark
        self.assertIn(f"{want['static_chars']}/{want['total_chars']}", out)

    def test_check_reads_utf16_with_a_byte_order_mark(self):
        # PowerShell 5's `>` writes UTF-16 LE with a mark
        for mark, codec in ((codecs.BOM_UTF16_LE, "utf-16-le"),
                            (codecs.BOM_UTF16_BE, "utf-16-be")):
            with self.subTest(codec):
                self._assert_read_as(mark + PROMPT.encode(codec), PROMPT)

    def test_check_reads_utf32_with_a_byte_order_mark(self):
        # the UTF-32 LE mark begins with the UTF-16 LE mark: it must not be
        # taken for UTF-16 (which would read NUL characters into the prompt)
        for mark, codec in ((codecs.BOM_UTF32_LE, "utf-32-le"),
                            (codecs.BOM_UTF32_BE, "utf-32-be")):
            with self.subTest(codec):
                self._assert_read_as(mark + PROMPT.encode(codec), PROMPT)

    def test_check_reads_utf8_with_and_without_a_mark(self):
        # `Set-Content -Encoding utf8` writes a mark; it is not part of the prompt
        self._assert_read_as(codecs.BOM_UTF8 + PROMPT.encode("utf-8"), PROMPT)
        self._assert_read_as(PROMPT.encode("utf-8"), PROMPT)

    def test_make_writes_plain_utf8_whatever_the_input_was(self):
        out_path = os.path.join(self.tmp, "o.txt")
        for mark, codec in ((codecs.BOM_UTF16_LE, "utf-16-le"),
                            (codecs.BOM_UTF8, "utf-8")):
            with self.subTest(codec):
                path = self._file(mark + PROMPT.encode(codec))
                rc, out, err = _cli("cache", "make", "-i", path,
                                    "-o", out_path, "--provider", "openai")
                self.assertEqual((rc, err), (0, ""))
                with open(out_path, "rb") as f:
                    data = f.read()
                for any_mark in (codecs.BOM_UTF8, codecs.BOM_UTF16_LE,
                                 codecs.BOM_UTF16_BE):
                    self.assertFalse(data.startswith(any_mark))
                text = data.decode("utf-8")
                self.assertIn("You are a", text)
                self.assertIn("{{role}}", text)
                self.assertNotIn("\x00", text)

    def test_latin1_bytes_are_one_error_line(self):
        # the panel's repro: a Latin-1 e-acute in the prompt file
        path = self._file(b"caf" + bytes([0xE9]) + b" {{x}}\n")
        for cmd in ("check", "make"):
            with self.subTest(cmd):
                rc, out, err = _cli("cache", cmd, "-i", path)
                self.assertEqual((rc, out), (2, ""))
                line = self._one_error_line(err, path)
                self.assertIn("UTF-8", line)

    def test_utf16_without_a_mark_is_refused_not_misread(self):
        # every other byte is NUL, which is still valid UTF-8: it would be
        # analysed as a prompt full of NUL characters
        path = self._file(PROMPT.encode("utf-16-le"))
        rc, out, err = _cli("cache", "check", "-i", path)
        self.assertEqual((rc, out), (2, ""))
        line = self._one_error_line(err, path)
        self.assertIn("byte-order mark", line)

    def test_damaged_files_with_a_mark_are_refused(self):
        cases = {
            "utf16 cut in half": codecs.BOM_UTF16_LE + PROMPT.encode("utf-16-le")[:-1],
            "utf32 cut short": codecs.BOM_UTF32_LE + PROMPT.encode("utf-32-le")[:-2],
            "utf8 mark, then latin1": codecs.BOM_UTF8 + b"caf" + bytes([0xE9]),
        }
        for label, data in cases.items():
            with self.subTest(label):
                path = self._file(data)
                rc, out, err = _cli("cache", "check", "-i", path)
                self.assertEqual((rc, out), (2, ""))
                self._one_error_line(err, path)

    def test_line_endings_still_read_as_plain_newlines(self):
        # reading a text file always turned CRLF and a lone CR into "\n";
        # reading bytes must keep that, or `make` would write "\r\r\n" on Windows
        raw = "Rules:\r\nbe brief\rQ: {{q}}\r\n"
        plain = "Rules:\nbe brief\nQ: {{q}}\n"
        self.assertEqual(cacheopt.decode_prompt(raw.encode("utf-8")), plain)
        self._assert_read_as(raw.encode("utf-8"), plain)
        out_path = os.path.join(self.tmp, "o.txt")
        rc, _, err = _cli("cache", "make", "-i", self._file(raw.encode("utf-8")),
                          "-o", out_path)
        self.assertEqual((rc, err), (0, ""))
        with open(out_path, "rb") as f:
            self.assertNotIn(b"\r\r", f.read())


class TestCacheFilesThatCannotBeUsed(unittest.TestCase):
    """A path that cannot be read or written is one line and exit 2."""

    def setUp(self):
        self.tmp = _tmp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.prompt = os.path.join(self.tmp, "p.txt")
        with open(self.prompt, "w", encoding="utf-8") as f:
            f.write(PROMPT)

    def _assert_refused(self, rc, out, err, path):
        self.assertEqual((rc, out), (2, ""))
        lines = err.splitlines()
        self.assertEqual(len(lines), 1, err)
        self.assertTrue(lines[0].startswith("error: "), err)
        self.assertIn(path, lines[0])
        self.assertNotIn("Traceback", err)

    def test_output_into_a_missing_directory(self):
        target = os.path.join(self.tmp, "no", "such", "dir", "out.txt")
        rc, out, err = _cli("cache", "make", "-i", self.prompt, "-o", target)
        self._assert_refused(rc, out, err, target)
        self.assertIn("write", err)
        # nothing is printed as if it had worked, and nothing is created
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "no")))

    def test_output_path_is_a_directory(self):
        target = os.path.join(self.tmp, "adir")
        os.mkdir(target)
        rc, out, err = _cli("cache", "make", "-i", self.prompt, "-o", target)
        self._assert_refused(rc, out, err, target)

    def test_input_path_is_a_directory(self):
        target = os.path.join(self.tmp, "adir")
        os.mkdir(target)
        for cmd in ("check", "make"):
            with self.subTest(cmd):
                rc, out, err = _cli("cache", cmd, "-i", target)
                self._assert_refused(rc, out, err, target)

    def test_input_file_that_does_not_exist(self):
        target = os.path.join(self.tmp, "nope.txt")
        rc, out, err = _cli("cache", "check", "-i", target)
        self._assert_refused(rc, out, err, target)

    def test_input_that_may_not_be_read(self):
        # (root and Windows ignore file modes, so simulate the refusal)
        denied = PermissionError(errno.EACCES, "Permission denied", self.prompt)
        with mock.patch.object(cli_mod, "open", create=True, side_effect=denied):
            rc, out, err = _cli("cache", "check", "-i", self.prompt)
        self._assert_refused(rc, out, err, self.prompt)
        self.assertIn("read", err)

    def test_output_that_may_not_be_written(self):
        target = os.path.join(self.tmp, "o.txt")
        real_open = open

        def refuse_writing(path, mode="r", *args, **kwargs):
            if "w" in mode:
                raise PermissionError(errno.EACCES, "Permission denied", path)
            return real_open(path, mode, *args, **kwargs)

        with mock.patch.object(cli_mod, "open", create=True,
                               side_effect=refuse_writing):
            rc, out, err = _cli("cache", "make", "-i", self.prompt, "-o", target)
        self._assert_refused(rc, out, err, target)
        self.assertFalse(os.path.exists(target))


class TestCacheRealProcess(unittest.TestCase):
    """The same refusals, from the command line, with no traceback."""

    def setUp(self):
        self.tmp = _tmp()
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def _run(self, *args):
        return subprocess.run([PY, "-m", "shortleash", "cache", *args],
                              cwd=REPO, env=_child_env(),
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              timeout=60)

    def test_the_panels_repros(self):
        latin1 = os.path.join(self.tmp, "p.txt")
        with open(latin1, "wb") as f:
            f.write(b"caf" + bytes([0xE9]) + b" {{x}}\n")
        good = os.path.join(self.tmp, "good.txt")
        with open(good, "wb") as f:
            f.write(b"Hello {{name}}\n")
        missing = os.path.join(self.tmp, "missing", "dir", "out.txt")
        for label, args in (("check, Latin-1", ("check", "-i", latin1)),
                            ("make, Latin-1", ("make", "-i", latin1)),
                            ("make, missing output directory",
                             ("make", "-i", good, "-o", missing))):
            with self.subTest(label):
                p = self._run(*args)
                err = p.stderr.decode("utf-8", "replace")
                self.assertEqual(p.returncode, 2, err)
                self.assertEqual(p.stdout, b"")
                self.assertEqual(len(err.splitlines()), 1, err)
                self.assertNotIn("Traceback", err)

    def test_a_utf16_file_works_from_the_command_line(self):
        path = os.path.join(self.tmp, "ps5.txt")
        with open(path, "wb") as f:
            f.write(codecs.BOM_UTF16_LE + "Hello {{name}}\r\n".encode("utf-16-le"))
        p = self._run("check", "-i", path)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(p.stderr, b"")
        self.assertIn(b"placeholders : name", p.stdout)


# ---------------------------------------------------------------------
# L-2

def _exiting(code: int) -> list:
    return [PY, "-c", f"raise SystemExit({code})"]


class TestAChildsOwnVerdictNumbers(unittest.TestCase):
    """`shortleash run -- <command that exits 124>` exits 124 as well. The
    README says the exit code alone cannot tell that from the leash's own
    verdict, and that the receipt can; this pins what it relies on."""

    def test_the_childs_own_number_passes_through_and_is_not_a_breach(self):
        for n in (124, 125, 126, 143):
            with self.subTest(n):
                code, rec = _run_guard(Guard(quiet=True, anomaly=None),
                                       _exiting(n))
                self.assertEqual(code, n)
                self.assertEqual(rec["child_returncode"], n)
                self.assertFalse(rec["breached"])
                self.assertIsNone(rec["breach_reason"])
                self.assertEqual(rec["not_enforced"], [])

    def test_the_leashs_own_124_is_a_breach_in_the_receipt(self):
        g = Guard(timeout=0.3, grace_period=1.0, quiet=True, anomaly=None)
        code, rec = _run_guard(
            g, [PY, "-c", "import time; time.sleep(60)"])
        self.assertEqual(code, 124)
        self.assertTrue(rec["breached"])
        self.assertIn("time limit", rec["breach_reason"])

    def test_the_leashs_own_124_after_the_fact_is_a_breach_in_the_receipt(self):
        report = json.dumps({"type": "result", "subtype": "success",
                             "is_error": False, "result": "done",
                             "total_cost_usd": 50.0, "session_id": "s"})
        code, rec = _run_guard(
            Guard(budget=1.0, quiet=True, anomaly=None),
            [PY, "-c", "import sys; print(sys.argv[1])", report])
        self.assertEqual(code, 124)
        self.assertEqual(rec["child_returncode"], 0)
        self.assertTrue(rec["breached"])
        self.assertFalse(rec["breach_live"])

    def test_the_leashs_own_125_names_the_caps_it_could_not_check(self):
        code, rec = _run_guard(Guard(budget=1.0, quiet=True, anomaly=None),
                               _exiting(0))
        self.assertEqual(code, 125)
        self.assertEqual(rec["child_returncode"], 0)
        self.assertFalse(rec["breached"])
        self.assertEqual(rec["not_enforced"], ["budget"])

    def test_the_leashs_own_126_writes_no_receipt(self):
        tmp = _tmp()
        self.addCleanup(shutil.rmtree, tmp, True)
        receipt = os.path.join(tmp, "r.json")
        # a directory cannot be started (EISDIR on POSIX, access denied on Windows)
        rc, out, err = _cli("run", "--quiet", "--receipt", receipt, "--", tmp)
        self.assertEqual(rc, 126, err)
        self.assertFalse(os.path.exists(receipt))

    def test_the_readme_says_how_to_tell_them_apart(self):
        readme = _readme()
        table = readme.split("### Exit codes", 1)[1].split("## The receipt", 1)[0]
        flat = " ".join(table.split())
        for needle in ("itself exits `124`, `125`, `126` or `143`",
                       "The receipt tells them apart", "`breached`",
                       "`not_enforced`", "`child_returncode`"):
            self.assertIn(needle, flat)


# ---------------------------------------------------------------------
# L-3

INJECTED = ["--output-format", "stream-json", "--verbose"]


class TestOutputFormatInBothSpellings(unittest.TestCase):
    """When wrapping `claude` the leash adds `--output-format stream-json
    --verbose` unless the user chose a format: `--output-format json` and
    `--output-format=json` are the same choice."""

    def test_the_users_choice_is_kept_in_either_spelling(self):
        for given in (["--output-format", "json"],
                      ["--output-format", "stream-json", "--verbose"],
                      ["--output-format=json"],
                      ["--output-format=stream-json", "--verbose"]):
            with self.subTest(given):
                argv = ["claude", "-p", "do the thing"] + given
                self.assertEqual(cli_mod._maybe_inject_stream_json(argv), argv)

    def test_added_when_the_user_gave_no_format(self):
        argv = ["claude", "-p", "do the thing"]
        self.assertEqual(cli_mod._maybe_inject_stream_json(argv),
                         argv + INJECTED)

    def test_only_an_argument_that_is_the_flag_counts(self):
        # a prompt that mentions the flag, and flags that merely start the same
        for given in (["-p", "explain --output-format=json"],
                      ["-p", "x", "--output-formats=json"],
                      ["-p", "x", "--output-format-version", "2"]):
            with self.subTest(given):
                argv = ["claude"] + given
                self.assertEqual(cli_mod._maybe_inject_stream_json(argv),
                                 argv + INJECTED)

    def test_other_programs_are_left_alone(self):
        argv = ["python", "agent.py", "--other"]
        self.assertEqual(cli_mod._maybe_inject_stream_json(argv), argv)

    def test_the_command_the_leash_runs_has_one_output_format(self):
        seen = []

        def fake_run(self, cmd):
            seen.append(list(cmd))
            return 0

        for given, extra in ((["--output-format=json"], 0),
                             (["--output-format", "json"], 0),
                             ([], 1)):
            with self.subTest(given):
                del seen[:]
                with mock.patch.object(Guard, "run", autospec=True,
                                       side_effect=fake_run):
                    rc = cli_mod.main(["run", "--quiet", "--", "claude",
                                       "-p", "x"] + given)
                self.assertEqual(rc, 0)
                (cmd,) = seen
                formats = [a for a in cmd if a.startswith("--output-format")]
                self.assertEqual(len(formats), 1, cmd)
                self.assertEqual(cmd.count("--verbose"), extra, cmd)

    def test_the_readme_says_the_users_format_is_kept(self):
        flat = " ".join(_readme().split())
        self.assertIn("unless you pass `--output-format` yourself", flat)
        self.assertIn("`--output-format=json`", flat)


# ---------------------------------------------------------------------
# L-4

class TestMinHistory(unittest.TestCase):
    """`--min-history N` is how many past runs anomaly mode needs before it
    arms: at least one, because the baseline is a median of them."""

    def setUp(self):
        self.tmp = _tmp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.history = os.path.join(self.tmp, "h.jsonl")

    def _main(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        code = None
        with redirect_stdout(out), redirect_stderr(err):
            try:
                code = cli_mod.main(list(argv))
            except SystemExit as e:  # argparse's own usage error
                code = e.code
        return code, err.getvalue()

    def test_zero_and_negative_are_a_usage_error(self):
        for value in ("0", "-1", "-50"):
            with self.subTest(value):
                with mock.patch.object(
                        Guard, "run",
                        side_effect=AssertionError("the run must not start")):
                    code, err = self._main(
                        "run", "--anomaly", "--min-history", value,
                        "--history-path", self.history, "--",
                        PY, "-c", "pass")
                self.assertEqual(code, 2)
                self.assertIn("--min-history must be >= 1", err)
                self.assertIn(value, err)
                self.assertNotIn("Traceback", err)

    def test_it_is_checked_without_anomaly_mode_too(self):
        # a number that makes no sense is refused wherever it is typed
        with mock.patch.object(Guard, "run",
                               side_effect=AssertionError("must not start")):
            code, err = self._main("run", "--min-history", "0", "--",
                                   PY, "-c", "pass")
        self.assertEqual(code, 2)
        self.assertIn("--min-history must be >= 1", err)

    def test_one_is_accepted(self):
        with mock.patch.object(Guard, "run", autospec=True,
                               return_value=0) as run:
            code, err = self._main("run", "--anomaly", "--min-history", "1",
                                   "--quiet", "--", PY, "-c", "pass")
        self.assertEqual(code, 0, err)
        self.assertEqual(run.call_args[0][0].min_history, 1)

    def test_from_the_command_line_nothing_runs(self):
        marker = os.path.join(self.tmp, "ran")
        p = subprocess.run(
            [PY, "-m", "shortleash", "run", "--anomaly", "--min-history", "0",
             "--history-path", self.history, "--",
             PY, "-c", f"open({marker!r}, 'w').close()"],
            cwd=REPO, env=_child_env(), stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, timeout=60)
        err = p.stderr.decode("utf-8", "replace")
        self.assertEqual(p.returncode, 2, err)
        self.assertIn("--min-history must be >= 1", err)
        self.assertNotIn("Traceback", err)
        self.assertFalse(os.path.exists(marker))

    def test_no_history_is_never_a_baseline(self):
        # the layer below the flag: a caller that passes 0 (Guard(min_history=0))
        # gets "not armed", not the median of an empty list
        self.assertIsNone(history_mod.baseline("agent", path=self.history,
                                               min_runs=0))
        g = Guard(anomaly=3.0, min_history=0, quiet=True,
                  history_path=self.history)
        code, rec = _run_guard(g, _exiting(0))
        self.assertEqual(code, 0)
        self.assertFalse(rec["anomaly"]["armed"])
        self.assertTrue(any("not armed" in w for w in rec["warnings"]), rec)

    def test_the_help_and_the_readme_state_the_minimum(self):
        out = io.StringIO()
        with redirect_stdout(out):
            try:
                cli_mod.main(["run", "--help"])
            except SystemExit:
                pass
        self.assertIn("at least 1", " ".join(out.getvalue().split()))
        self.assertIn("`--min-history` is at least `1`",
                      " ".join(_readme().split()))


# ---------------------------------------------------------------------
# L-5

def _printer(*lines: str) -> list:
    """A child that prints the given lines and exits 0."""
    return [PY, "-c", "import sys\nfor l in sys.argv[1:]: print(l, flush=True)",
            *lines]


def _result(cost=None, usage=None, **extra) -> str:
    ev = {"type": "result", "subtype": "success", "is_error": False,
          "result": "done", "num_turns": 1, "session_id": "s"}
    if cost is not None:
        ev["total_cost_usd"] = cost
    if usage is not None:
        ev["usage"] = usage
    ev.update(extra)
    return json.dumps(ev)


def _assistant(mid: str, usage, spawns: int = 0) -> str:
    blocks = [{"type": "tool_use", "id": "%s-t%d" % (mid, i), "name": "Agent",
               "input": {}} for i in range(spawns)]
    return json.dumps({"type": "assistant", "message": {
        "id": mid, "content": blocks, "usage": usage}})


ALIEN = {"prompt_toks": 5000000, "completion_toks": 7}
GUARDRAILS = "finished within guardrails"


class TestUsageTheLeashCannotRead(unittest.TestCase):
    """Only input_tokens, output_tokens, cache_creation_input_tokens and
    cache_read_input_tokens are token counts to the leash. A `usage` block
    with fields but none of those is not usage: it cannot back a cap, so the
    cap is reported as not enforced (exit 125) instead of passing."""

    def _assert_not_a_pass(self, code, rec, cap):
        self.assertEqual(code, 125, rec)
        self.assertEqual(rec["not_enforced"], [cap])
        self.assertNotIn(cap, rec["enforced"])
        self.assertFalse(rec["breached"])
        self.assertNotIn(GUARDRAILS, receipt_mod.format_human(rec))
        warned = [w for w in rec["warnings"] if "prompt_toks" in w]
        self.assertEqual(len(warned), 1, rec["warnings"])
        self.assertIn("input_tokens", warned[0])  # says what it does read

    def test_the_panels_repro_a_final_report_in_other_words(self):
        g = Guard(max_tokens=100, quiet=True, anomaly=None)
        code, rec = _run_guard(g, _printer(_result(usage=ALIEN)))
        self._assert_not_a_pass(code, rec, "max_tokens")
        self.assertEqual(rec["tokens"]["input"], 0)

    def test_the_same_in_every_event_of_the_stream(self):
        g = Guard(max_tokens=100, quiet=True, anomaly=None)
        code, rec = _run_guard(g, _printer(_assistant("m1", ALIEN),
                                           _assistant("m2", ALIEN),
                                           _result(usage=ALIEN)))
        self._assert_not_a_pass(code, rec, "max_tokens")
        self.assertEqual(rec["usage_events"], 0)

    def test_a_cost_the_agent_reported_still_backs_the_budget(self):
        # the usage block is unreadable but total_cost_usd is a plain number
        g = Guard(budget=1.0, max_tokens=100, quiet=True, anomaly=None)
        code, rec = _run_guard(g, _printer(_result(cost=0.5, usage=ALIEN)))
        self._assert_not_a_pass(code, rec, "max_tokens")
        self.assertEqual(rec["enforced"], ["budget"])
        self.assertEqual(rec["cost_usd"], 0.5)

    def test_a_reported_cost_over_the_budget_is_still_a_breach(self):
        g = Guard(budget=1.0, quiet=True, anomaly=None)
        code, rec = _run_guard(g, _printer(_result(cost=50, usage=ALIEN)))
        self.assertEqual(code, 124)
        self.assertTrue(rec["breached"])
        self.assertIn("budget exceeded after the fact", rec["breach_reason"])

    def test_with_no_cap_on_it_only_a_warning_is_added(self):
        g = Guard(timeout=30.0, quiet=True, anomaly=None)
        code, rec = _run_guard(g, _printer(_result(cost=0.5, usage=ALIEN)))
        self.assertEqual(code, 0)
        self.assertEqual(rec["not_enforced"], [])
        self.assertEqual(len([w for w in rec["warnings"]
                              if "prompt_toks" in w]), 1)

    def test_one_readable_field_among_unknown_ones_is_usage(self):
        usage = {"input_tokens": 500, "prompt_toks": 7}
        g = Guard(max_tokens=100, quiet=True, anomaly=None)
        code, rec = _run_guard(g, _printer(_result(usage=usage)))
        self.assertEqual(code, 124)
        self.assertIn("token cap exceeded after the fact",
                      rec["breach_reason"])
        self.assertEqual(rec["warnings"], [])

    def test_a_real_zero_is_still_usage(self):
        g = Guard(max_tokens=100, quiet=True, anomaly=None)
        code, rec = _run_guard(
            g, _printer(_result(usage={"input_tokens": 0, "output_tokens": 0})))
        self.assertEqual(code, 0)
        self.assertEqual(rec["enforced"], ["max_tokens"])
        self.assertEqual(rec["not_enforced"], [])

    def test_the_parser_names_the_fields_it_could_not_read(self):
        obs = events.parse_line(_result(usage=ALIEN))
        self.assertIsNone(obs["usage"])
        self.assertEqual(obs["usage_unreadable"],
                         ["completion_toks", "prompt_toks"])
        obs = events.parse_line(_assistant("m", ALIEN))
        self.assertEqual(obs["usage_unreadable"],
                         ["completion_toks", "prompt_toks"])
        # readable, empty or absent: nothing to report
        for usage in ({"input_tokens": 1}, {"input_tokens": 1, "other": 2}, {}):
            for line in (_result(usage=usage), _assistant("m", usage)):
                with self.subTest(line[:40] + str(usage)):
                    self.assertIsNone(events.parse_line(line)["usage_unreadable"])
        self.assertIsNone(events.parse_line(_result())["usage_unreadable"])

    def test_each_of_the_four_documented_fields_is_usage(self):
        # the README's list, spelled out here on purpose
        for field in ("input_tokens", "output_tokens",
                      "cache_creation_input_tokens", "cache_read_input_tokens"):
            with self.subTest(field):
                for line in (_result(usage={field: 5}),
                             _assistant("m", {field: 5})):
                    self.assertIsNone(
                        events.parse_line(line)["usage_unreadable"])
                g = Guard(max_tokens=1, quiet=True, anomaly=None)
                code, rec = _run_guard(g, _printer(_result(usage={field: 5})))
                self.assertEqual(code, 124, rec)

    def test_the_names_in_the_warning_are_bounded(self):
        usage = {"field%02d" % i: i for i in range(50)}
        obs = events.parse_line(_result(usage=usage))
        self.assertLessEqual(len(obs["usage_unreadable"]), 8)

    def test_readme_says_so(self):
        readme = _readme()
        flat = " ".join(readme.split())
        for needle in ("input_tokens", "cache_creation_input_tokens",
                       "none of those fields", "`not_enforced`",
                       "Before 0.1.2 such a block counted as zero tokens"):
            self.assertIn(needle, flat)
        # ... and the exit-code table's 125 row points at it
        table = readme.split("### Exit codes", 1)[1].split("## The receipt", 1)[0]
        row = [r for r in table.splitlines() if r.startswith("| `125`")][0]
        self.assertIn("No telemetry, no comfort", row)


# ---------------------------------------------------------------------
# L-6

class TestTheEchoIsABestEffortCopy(unittest.TestCase):
    """What the README now says about the agent's output on our stdout."""

    def _echoed(self, child_code: str) -> bytes:
        env = _child_env()
        env["PYTHONIOENCODING"] = "utf-8"  # (a Windows pipe would say cp1252)
        p = subprocess.run(
            [PY, "-m", "shortleash", "run", "--quiet", "--",
             PY, "-c", child_code],
            cwd=REPO, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            timeout=60)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertNotIn(b"Traceback", p.stderr)
        # on Windows the echo is written in text mode: every line ends in
        # CR LF (the README says so)
        return p.stdout.replace(b"\r\n", b"\n")

    def test_carriage_returns_end_a_line_and_come_out_as_newlines(self):
        out = self._echoed(
            "import sys; sys.stdout.buffer.write(b'a\\rb\\r\\nc\\n')")
        self.assertEqual(out, b"a\nb\nc\n")

    def test_a_progress_bar_becomes_many_lines(self):
        out = self._echoed(
            "import sys; sys.stdout.buffer.write(b'10%\\r50%\\r100%\\n')")
        self.assertEqual(out.splitlines(), [b"10%", b"50%", b"100%"])

    def test_bytes_that_are_not_utf8_come_out_as_the_replacement_character(self):
        out = self._echoed(
            "import sys; sys.stdout.buffer.write(b'caf' + bytes([0xE9]) + b'!\\n')")
        self.assertEqual(out, b"caf" + chr(0xFFFD).encode("utf-8") + b"!\n")

    def test_claudes_json_lines_come_through_unchanged(self):
        line = json.dumps({"type": "system", "subtype": "init",
                           "session_id": "s", "model": "m", "note": "caf" + chr(0xE9)},
                          ensure_ascii=False)
        out = self._echoed("import sys; sys.stdout.buffer.write("
                           + repr((line + "\n").encode("utf-8")) + ")")
        self.assertEqual(out, (line + "\n").encode("utf-8"))

    def test_a_full_queue_drops_lines_and_counts_them(self):
        # not only for a reader that has stopped: anything that is slower
        # than the agent writes, for long enough, fills the 4 MB queue
        release = threading.Event()
        pump = _EchoPump(lambda _line: release.wait(30), limit=1000)
        self.addCleanup(release.set)
        for _ in range(5000):
            pump.put("x" * 99 + "\n")
        self.assertGreater(pump.dropped, 4000)

    def test_the_readme_describes_the_echo_as_it_is(self):
        flat = " ".join(_readme().split())
        self.assertNotIn("passes through untouched", flat)
        for needle in ("best-effort copy", "U+FFFD", "carriage return",
                       "plain `\\n`", "each line ends in CR LF",
                       "faster than a reader can take it",
                       "were not echoed"):
            self.assertIn(needle, flat)


if __name__ == "__main__":
    unittest.main()
