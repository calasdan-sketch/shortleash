"""Round 7 (third verification panel): regression tests, one class per finding.

L-1 `import sqlite3` sat at module level although only `--fleet` needs it: on
    a Python built without `_sqlite3` (pyenv, Alpine) even `shortleash
    --version` died with an ImportError traceback. Nothing but the fleet
    imports it now, and the fleet says so in one line when it is missing.
L-2 One byte that is not UTF-8 in history.jsonl (a torn write, a hand edit) made
    `shortleash run --anomaly` end in a UnicodeDecodeError traceback before the
    agent started, although a corrupt line is meant to be a warning.
L-4 `--pricing` was read as strict UTF-8: a file with a byte-order mark
    (PowerShell 5 `Out-File`, Notepad) or in UTF-16 (PowerShell 5 `>`) was
    refused, while `cache check|make -i` has read both since 0.1.2. One shared
    decoder (textfile.decode_text) now serves both.
L-5 `agent_name` was case-sensitive: on a case-insensitive file system `Claude
    -p hi` or `CLAUDE.EXE` starts claude, but `--output-format stream-json
    --verbose` was not added, so no telemetry and exit 125. The name is now
    compared (and used as the receipt / history key) in lower case.
L-3 The sdist shipped tests/test_*.py but not tests/_isolate.py or
    sim/runaway.py, so `python -m unittest discover -s tests` in the unpacked
    archive gave 11 import errors. MANIFEST.in ships both.
L-6 README / --help accuracy: the `fleet list` example said `held by dan`
    without the `--fleet-owner dan` that makes it so (the default is `?`);
    `--fleet-ttl` above its documented maximum was silently clamped (it is
    refused now, like every other out-of-range option); `--history-path` and
    `--quiet` were in `--help` only; "echoed only about 5 MB" is a figure
    from one machine.
L-7 The model is read from the stream's system/init event only (a limit the
    README now states; the test pins that the code really behaves so).

Real-subprocess tests remove PYTHONUNBUFFERED from the child's environment (it
hides buffered-flush failures) and start it with the default signals.
"""

import _isolate  # noqa: F401  (isolate ~/.shortleash before importing)

import argparse
import codecs
import io
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

import shortleash
from shortleash import cacheopt
from shortleash import cli as cli_mod
from shortleash import fleet as fleet_mod
from shortleash import history as history_mod
from shortleash import pricing
from shortleash import proctree
from shortleash.guard import _EXE_SUFFIXES, Guard, agent_name

PY = sys.executable
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
POSIX = not proctree.is_windows()


def _tmp() -> str:
    return tempfile.mkdtemp(prefix="sl7-")


def _rmtree(path: str) -> None:
    shutil.rmtree(path, ignore_errors=True)


def _child_env() -> dict:
    env = dict(os.environ)
    env.pop("PYTHONUNBUFFERED", None)
    return env


def _cli(*argv):
    """Run the CLI in this process: (exit code, stdout, stderr)."""
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        try:
            rc = cli_mod.main(list(argv))
        except SystemExit as e:  # argparse: a bad option value, --version
            rc = e.code
    return rc, out.getvalue(), err.getvalue()


def printer(*lines: str) -> list:
    """A child that prints the given lines and exits 0."""
    return [PY, "-c", "import sys\nfor l in sys.argv[1:]: print(l, flush=True)",
            *lines]


# ---------------------------------------------------------------------
# L-1

def _run_without_sqlite(*cli_args, blocked="_sqlite3"):
    """Start the CLI in a fresh interpreter in which `import sqlite3` fails
    the way it does on a Python built without the C module: (exit code,
    stdout, stderr). ``blocked`` is the module name that cannot be imported
    ("_sqlite3": the real failure inside the sqlite3 package; "sqlite3": the
    panel's simulation)."""
    code = ("import sys\n"
            f"sys.modules[{blocked!r}] = None\n"
            "from shortleash import cli\n"
            "sys.exit(cli.main())\n")
    p = subprocess.run([PY, "-c", code, *cli_args], cwd=REPO,
                       env=_child_env(), stdout=subprocess.PIPE,
                       stderr=subprocess.PIPE, timeout=120,
                       preexec_fn=_isolate.DEFAULT_SIGNALS)
    return (p.returncode, p.stdout.decode("utf-8", "replace"),
            p.stderr.decode("utf-8", "replace"))


class TestSqliteIsOnlyNeededByTheFleet(unittest.TestCase):
    def test_importing_the_package_does_not_import_sqlite3(self):
        # the cause of the traceback: a module-level import nothing else needs
        code = ("import sys\n"
                "import shortleash.cli, shortleash.guard, shortleash.history\n"
                "import shortleash.fleet, shortleash.pricing\n"
                "bad = [m for m in ('sqlite3', '_sqlite3') if m in sys.modules]\n"
                "sys.exit(1 if bad else 0)\n")
        p = subprocess.run([PY, "-c", code], cwd=REPO, env=_child_env(),
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                           timeout=120, preexec_fn=_isolate.DEFAULT_SIGNALS)
        self.assertEqual(p.returncode, 0, p.stderr.decode("utf-8", "replace"))

    def test_version_works_without_sqlite3(self):
        # the panel's repro: --version died with an ImportError traceback
        for blocked in ("_sqlite3", "sqlite3"):
            with self.subTest(blocked):
                rc, out, err = _run_without_sqlite("--version", blocked=blocked)
                self.assertEqual((rc, err), (0, ""))
                self.assertIn(shortleash.__version__, out)

    def test_cache_works_without_sqlite3(self):
        rc, out, err = _run_without_sqlite("cache", "guide")
        self.assertEqual((rc, err), (0, ""))
        self.assertIn("PROMPT CACHING", out)

    def test_a_run_without_the_fleet_works_without_sqlite3(self):
        tmp = _tmp()
        self.addCleanup(_rmtree, tmp)
        rp = os.path.join(tmp, "r.json")
        rc, out, err = _run_without_sqlite(
            "run", "--quiet", "--receipt", rp, "--",
            *printer("the-agent-ran"))
        self.assertEqual(rc, 0, err)
        self.assertIn("the-agent-ran", out)
        self.assertNotIn("Traceback", err)
        with open(rp, encoding="utf-8") as f:
            self.assertFalse(json.load(f)["breached"])

    def test_fleet_list_is_one_error_line_and_exit_2(self):
        for blocked in ("_sqlite3", "sqlite3"):
            with self.subTest(blocked):
                rc, out, err = _run_without_sqlite("fleet", "list",
                                                   blocked=blocked)
                self.assertEqual((rc, out), (2, ""))
                lines = err.splitlines()
                self.assertEqual(len(lines), 1, err)
                self.assertTrue(lines[0].startswith("shortleash: error: "), err)
                self.assertIn("sqlite3", lines[0])
                self.assertNotIn("Traceback", err)

    def test_run_with_fleet_goes_ahead_without_a_claim(self):
        # a claim is advisory: no sqlite3 is a warning, never a reason not to
        # run (the same rule as an unusable fleet.db)
        tmp = _tmp()
        self.addCleanup(_rmtree, tmp)
        rp = os.path.join(tmp, "r.json")
        rc, out, err = _run_without_sqlite(
            "run", "--fleet", "--quiet", "--receipt", rp, "--",
            *printer("the-agent-ran"))
        self.assertEqual(rc, 0, err)
        self.assertIn("the-agent-ran", out)
        self.assertIn("fleet WARNING", err)
        self.assertIn("sqlite3", err)
        self.assertIn("WITHOUT a claim", err)
        self.assertNotIn("Traceback", err)
        with open(rp, encoding="utf-8") as f:
            warnings = json.load(f)["warnings"]
        self.assertTrue(any("sqlite3" in w for w in warnings), warnings)

    def test_in_process_the_fleet_reports_a_missing_module(self):
        with mock.patch.dict(sys.modules, {"sqlite3": None}):
            with self.assertRaises(fleet_mod.FleetUnavailable) as cm:
                fleet_mod.connect(os.path.join(_tmp(), "fleet.db"))
            self.assertIn("sqlite3", str(cm.exception))
            # the errors that mean "database unusable" still include OSError
            self.assertIn(OSError, fleet_mod.db_errors())
        import sqlite3
        self.assertIn(sqlite3.Error, fleet_mod.db_errors())

    def test_in_process_fleet_list_and_run_fleet(self):
        with mock.patch.dict(sys.modules, {"sqlite3": None}):
            rc, out, err = _cli("fleet", "list")
            self.assertEqual((rc, out), (2, ""))
            self.assertEqual(len(err.splitlines()), 1, err)
            self.assertIn("sqlite3", err)
            rc, out, err = _cli("run", "--fleet", "--quiet", "--",
                                *printer("the-agent-ran"))
            self.assertEqual(rc, 0, err)
            self.assertIn("the-agent-ran", out)
            self.assertIn("WITHOUT a claim", err)


# ---------------------------------------------------------------------
# L-2

def _history_line(agent="a", cost=1.0, tokens=100) -> bytes:
    return (json.dumps({"agent": agent, "profile": "", "cost_usd": cost,
                        "tokens": tokens, "breached": False,
                        "ended_at": "2026-10-18T00:00:00+00:00"})
            + "\n").encode("utf-8")


class TestAHistoryWithBytesThatAreNotUtf8(unittest.TestCase):
    def setUp(self):
        self.tmp = _tmp()
        self.addCleanup(_rmtree, self.tmp)
        self.path = os.path.join(self.tmp, "history.jsonl")

    def _write(self, *chunks: bytes) -> None:
        with open(self.path, "wb") as f:
            for c in chunks:
                f.write(c)

    def test_the_panels_repro_bytes_are_one_corrupt_line(self):
        # printf '\xff\xfe\n' >> $SHORTLEASH_HOME/history.jsonl
        self._write(*[_history_line() for _ in range(5)], b"\xff\xfe\n")
        warnings = []
        base = history_mod.baseline("a", path=self.path, warnings=warnings)
        self.assertEqual(base["runs"], 5)
        self.assertEqual(warnings,
                         [f"history: skipped 1 corrupt line(s) in {self.path}"])

    def test_a_valid_looking_line_with_a_bad_byte_is_corrupt_too(self):
        # JSON that parses once the bad byte is replaced is still a damaged
        # line: it must not count as a run, and must be counted as corrupt
        damaged = (b'{"agent": "a", "profile": "", "cost_usd": 1.0, '
                   b'"tokens": 100, "breached": false, "note": "caf\xe9"}\n')
        self._write(*[_history_line() for _ in range(5)], damaged)
        warnings = []
        base = history_mod.baseline("a", path=self.path, warnings=warnings)
        self.assertEqual(base["runs"], 5)
        self.assertEqual(len(warnings), 1, warnings)
        self.assertIn("skipped 1 corrupt line(s)", warnings[0])

    def test_good_lines_around_the_damage_still_count(self):
        # a torn write leaves a cut multi-byte sequence in the middle
        self._write(_history_line(), b'{"agent": "caf\xc3', b"\n",
                    _history_line(), _history_line(), b"\x80\x81\x82\n",
                    _history_line())
        warnings = []
        costs, tokens = history_mod._runs("a", "", self.path, warnings)
        self.assertEqual(costs, [1.0, 1.0, 1.0, 1.0])
        self.assertEqual(tokens, [100] * 4)
        self.assertIn("skipped 2 corrupt line(s)", warnings[0])

    def test_count_runs_survives_it_too(self):
        self._write(_history_line(), b"\xff\xfe\n", _history_line())
        self.assertEqual(history_mod.count_runs("a", path=self.path), 2)

    def test_run_anomaly_starts_the_agent_and_warns(self):
        # the panel's repro, end to end, through the default history path
        home = os.path.join(self.tmp, "home")
        os.makedirs(home)
        with open(os.path.join(home, "history.jsonl"), "wb") as f:
            f.write(b"\xff\xfe\n")
        env = _child_env()
        env["SHORTLEASH_HOME"] = home
        p = subprocess.run(
            [PY, "-m", "shortleash", "run", "--anomaly", "--",
             *printer("the-agent-ran")],
            cwd=REPO, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            timeout=120, preexec_fn=_isolate.DEFAULT_SIGNALS)
        out = p.stdout.decode("utf-8", "replace")
        err = p.stderr.decode("utf-8", "replace")
        self.assertEqual(p.returncode, 0, err)
        self.assertIn("the-agent-ran", out)
        self.assertNotIn("Traceback", err)
        self.assertNotIn("UnicodeDecodeError", err)
        self.assertIn("skipped 1 corrupt line(s)", err)


# ---------------------------------------------------------------------
# L-4

# CRLF, as PowerShell writes it
PRICING_TEXT = '{"as_of": "2026-10-18",\r\n "claude-test": [1.5, 7.5]}\r\n'

#: What a Windows tool may have written (label -> the file's bytes).
PRICING_FILES = {
    "utf-8": PRICING_TEXT.encode("utf-8"),
    "utf-8 with a mark (Out-File, Notepad)":
        codecs.BOM_UTF8 + PRICING_TEXT.encode("utf-8"),
    "utf-16 le with a mark (PowerShell 5 '>')":
        codecs.BOM_UTF16_LE + PRICING_TEXT.encode("utf-16-le"),
    "utf-16 be with a mark":
        codecs.BOM_UTF16_BE + PRICING_TEXT.encode("utf-16-be"),
    "utf-32 le with a mark":
        codecs.BOM_UTF32_LE + PRICING_TEXT.encode("utf-32-le"),
    "utf-32 be with a mark":
        codecs.BOM_UTF32_BE + PRICING_TEXT.encode("utf-32-be"),
}


class TestThePricingFileIsReadLikeAPromptFile(unittest.TestCase):
    def setUp(self):
        self.tmp = _tmp()
        self.addCleanup(_rmtree, self.tmp)

    def _file(self, data: bytes, name: str = "pricing.json") -> str:
        path = os.path.join(self.tmp, name)
        with open(path, "wb") as f:
            f.write(data)
        return path

    def test_load_table_reads_every_encoding_a_windows_tool_writes(self):
        for label, data in PRICING_FILES.items():
            with self.subTest(label):
                table = pricing.load_table(self._file(data))
                self.assertEqual(table.as_of, "2026-10-18")
                self.assertEqual(table.rates["claude-test"], (1.5, 7.5))

    def test_the_environment_variable_file_too(self):
        for label, data in PRICING_FILES.items():
            with self.subTest(label):
                table = pricing.load_table(
                    None, environ={pricing.PRICING_ENV: self._file(data)})
                self.assertEqual(table.rates["claude-test"], (1.5, 7.5))

    def test_run_uses_it(self):
        # the panel's repro: --pricing FILE with a mark / in UTF-16 was rc 2
        for label, data in PRICING_FILES.items():
            with self.subTest(label):
                rp = os.path.join(self.tmp, "r.json")
                rc, out, err = _cli(
                    "run", "--pricing", self._file(data), "--quiet",
                    "--receipt", rp, "--history-path",
                    os.path.join(self.tmp, "h.jsonl"), "--",
                    *printer("the-agent-ran"))
                self.assertEqual((rc, err), (0, ""))
                self.assertIn("the-agent-ran", out)
                with open(rp, encoding="utf-8") as f:
                    self.assertEqual(json.load(f)["pricing"]["as_of"],
                                     "2026-10-18")

    def _refused(self, data: bytes):
        path = self._file(data)
        rc, out, err = _cli("run", "--pricing", path, "--",
                            *printer("the-agent-ran"))
        self.assertEqual((rc, out), (2, ""))
        lines = err.splitlines()
        self.assertEqual(len(lines), 1, err)
        self.assertTrue(lines[0].startswith(
            "shortleash: error: can't load pricing file "), err)
        self.assertIn(path, lines[0])
        self.assertNotIn("Traceback", err)
        return lines[0]

    def test_latin1_bytes_are_one_error_line_naming_the_encodings(self):
        line = self._refused(b'{"caf\xe9": [1, 2]}')
        self.assertIn("UTF-8", line)
        self.assertIn("byte-order mark", line)

    def test_utf16_without_a_mark_is_refused_not_misread(self):
        line = self._refused(PRICING_TEXT.encode("utf-16-le"))
        self.assertIn("byte-order mark", line)

    def test_damaged_files_with_a_mark_are_refused(self):
        cut16 = codecs.BOM_UTF16_LE + PRICING_TEXT.encode("utf-16-le")[:-1]
        cut32 = codecs.BOM_UTF32_LE + PRICING_TEXT.encode("utf-32-le")[:-2]
        for label, data in (("utf-16 cut in half", cut16),
                            ("utf-32 cut short", cut32)):
            with self.subTest(label):
                self._refused(data)

    def test_json_that_is_wrong_is_still_reported_as_before(self):
        # (a mark, then JSON cut short: the JSON error is what is reported)
        line = self._refused(codecs.BOM_UTF8 + b'{"sonnet": [3, 15],')
        self.assertIn("Expecting", line)
        self.assertIn("line 1", line)

    def test_one_decoder_serves_the_pricing_file_and_the_prompt_file(self):
        from shortleash import textfile
        with mock.patch.object(textfile, "decode_text",
                               side_effect=ValueError("sentinel")):
            with self.assertRaises(ValueError) as cm:
                pricing.load_table(self._file(b"{}"))
            self.assertIn("sentinel", str(cm.exception))
            with self.assertRaises(ValueError) as cm:
                cacheopt.decode_prompt(b"x")
            self.assertIn("sentinel", str(cm.exception))

    def test_the_prompt_file_decoding_is_unchanged(self):
        # (the cache tests pin the messages; this pins the newline folding)
        data = codecs.BOM_UTF16_LE + "a\r\nb\rc\n".encode("utf-16-le")
        self.assertEqual(cacheopt.decode_prompt(data), "a\nb\nc\n")


# ---------------------------------------------------------------------
# L-5

#: Ways to start claude that a case-insensitive file system accepts.
CLAUDE_SPELLINGS = [
    "claude", "Claude", "CLAUDE", "claude.exe", "CLAUDE.EXE", "Claude.Exe",
    "claude.cmd", "Claude.CMD", "claude.bat", "CLAUDE.BAT",
    r"C:\Users\Dan\AppData\Roaming\npm\Claude.cmd",
    "C:/Program Files/Claude/CLAUDE.EXE",
    "/usr/local/bin/Claude", "./CLAUDE",
]

STREAM_JSON = ["--output-format", "stream-json", "--verbose"]


class TestAgentNamesAreCaseInsensitive(unittest.TestCase):
    def test_the_name_is_lower_case_without_a_windows_suffix(self):
        for argv0 in CLAUDE_SPELLINGS:
            with self.subTest(argv0):
                self.assertEqual(agent_name(argv0), "claude")

    def test_other_names_keep_their_shape_but_not_their_case(self):
        self.assertEqual(agent_name("MyAgent.EXE"), "myagent")
        self.assertEqual(agent_name(r"C:\Tools\Python311\PYTHON.exe"), "python")
        self.assertEqual(agent_name("agent.sh"), "agent.sh")
        # a bare suffix is a name, as before
        self.assertEqual(agent_name(".EXE"), ".exe")
        self.assertEqual(agent_name(".exe"), ".exe")

    def test_the_stream_json_flags_are_added_whatever_the_spelling(self):
        # the panel's repro: `Claude -p hi` got no flags and ended in exit 125
        for argv0 in CLAUDE_SPELLINGS:
            with self.subTest(argv0):
                argv = [argv0, "-p", "hi"]
                self.assertEqual(cli_mod._maybe_inject_stream_json(argv),
                                 argv + STREAM_JSON)

    def test_the_users_own_output_format_is_kept_in_any_spelling(self):
        for argv0 in ("Claude", "CLAUDE.EXE", "claude.CMD"):
            for given in (["--output-format", "json"], ["--output-format=json"]):
                with self.subTest(argv0, given=given):
                    argv = [argv0, "-p", "hi", *given]
                    self.assertEqual(cli_mod._maybe_inject_stream_json(argv),
                                     argv)

    def test_only_claude_gets_them(self):
        for argv0 in ("claudette", "claude-code", "myclaude", "claude.sh",
                      "claude2", "Claude Code.exe", "python", "PYTHON.EXE"):
            with self.subTest(argv0):
                argv = [argv0, "-p", "hi"]
                self.assertEqual(cli_mod._maybe_inject_stream_json(argv), argv)

    @unittest.skipUnless(POSIX, "a script named Claude with no extension is "
                         "only runnable on a POSIX file system")
    def test_a_program_really_named_with_capitals_gets_the_flags(self):
        # end to end: the arguments the program receives, through run()
        tmp = _tmp()
        self.addCleanup(_rmtree, tmp)
        stub = os.path.join(tmp, "Claude")
        body = "import sys, json; print(json.dumps(sys.argv[1:]))"
        with open(stub, "w", encoding="utf-8", newline="\n") as f:
            f.write("#!/bin/sh\nexec %s -c %s \"$@\"\n"
                    % (shlex.quote(PY), shlex.quote(body)))
        os.chmod(stub, 0o755)
        rc, out, err = _cli("run", "--quiet", "--history-path",
                            os.path.join(tmp, "h.jsonl"), "--", stub, "-p", "hi")
        self.assertEqual(json.loads(out.strip().splitlines()[-1]),
                         ["-p", "hi"] + STREAM_JSON, (out, err))


# ---------------------------------------------------------------------
# L-3

def _setuptools_major() -> int:
    """The major version of the setuptools this interpreter would build the
    sdist with (0 when there is none)."""
    p = subprocess.run([PY, "-c", "import setuptools; "
                        "print(setuptools.__version__)"], cwd=REPO,
                       env=_child_env(), stdout=subprocess.PIPE,
                       stderr=subprocess.PIPE, timeout=120,
                       preexec_fn=_isolate.DEFAULT_SIGNALS)
    try:
        return int(p.stdout.decode("ascii", "replace").split(".")[0])
    except ValueError:
        return 0


def _manifest_lines() -> list:
    with open(os.path.join(REPO, "MANIFEST.in"), encoding="utf-8") as f:
        return [l.strip() for l in f.read().splitlines()
                if l.strip() and not l.lstrip().startswith("#")]


class TestTheManifest(unittest.TestCase):
    def test_it_ships_the_tests_and_the_fake_agent_they_run(self):
        lines = _manifest_lines()
        self.assertIn("graft tests", lines)
        self.assertIn("graft sim", lines)

    def test_it_leaves_compiled_files_out(self):
        # `graft tests` takes whatever lies in tests/, caches included
        self.assertIn("global-exclude *.py[cod]", _manifest_lines())


class TestTheSdistRunsItsOwnTests(unittest.TestCase):
    """Build the sdist from a copy of this tree, unpack it, and run tests in
    it: what the panel did by hand."""

    @classmethod
    def setUpClass(cls):
        if _setuptools_major() < 77:
            raise unittest.SkipTest(
                "building an sdist needs the setuptools (>= 77) that "
                "pyproject.toml asks for; this interpreter has none")
        cls.tmp = _tmp()
        cls.addClassCleanup(_rmtree, cls.tmp)
        src = os.path.join(cls.tmp, "src")
        shutil.copytree(REPO, src, ignore=shutil.ignore_patterns(
            ".git", ".venv", "build", "dist", "*.egg-info", ".pytest_cache",
            "__pycache__", "*.pyc"))
        # what a test run leaves in the tree the sdist is built from
        litter = os.path.join(src, "tests", "__pycache__")
        os.makedirs(litter)
        with open(os.path.join(litter, "test_x.cpython-310.pyc"), "wb") as f:
            f.write(b"\0")
        out = os.path.join(cls.tmp, "dist")
        os.makedirs(out)
        p = subprocess.run(
            [PY, "-c", "import sys, setuptools.build_meta as b; "
             "print(b.build_sdist(sys.argv[1]))", out], cwd=src,
            env=_child_env(), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            timeout=300, preexec_fn=_isolate.DEFAULT_SIGNALS)
        log = p.stdout.decode("utf-8", "replace")
        assert p.returncode == 0, log
        cls.archive = os.path.join(out, log.strip().splitlines()[-1])
        cls.unpacked = os.path.join(cls.tmp, "unpacked")
        with tarfile.open(cls.archive) as tf:
            members = tf.getmembers()
            for m in members:
                target = os.path.join(cls.unpacked, *m.name.split("/"))
                if m.isdir():
                    os.makedirs(target, exist_ok=True)
                elif m.isfile():
                    os.makedirs(os.path.dirname(target), exist_ok=True)
                    with open(target, "wb") as f:
                        f.write(tf.extractfile(m).read())
            cls.names = sorted(m.name.split("/", 1)[1] for m in members
                               if "/" in m.name)
        cls.root = os.path.join(cls.unpacked, os.listdir(cls.unpacked)[0])

    def test_it_holds_every_test_file_and_what_they_import(self):
        want = ["tests/" + n for n in sorted(os.listdir(
            os.path.join(REPO, "tests"))) if n.endswith(".py")]
        want += ["sim/runaway.py", "README.md", "LICENSE", "pyproject.toml"]
        self.assertIn("tests/_isolate.py", want)
        self.assertEqual([n for n in want if n not in self.names], [])

    def test_it_holds_no_compiled_files(self):
        self.assertEqual([n for n in self.names
                          if "__pycache__" in n or n.endswith(".pyc")], [])

    def test_its_tests_run(self):
        # test_history needs both _isolate and the fake agent in sim/
        p = subprocess.run(
            [PY, "-m", "unittest", "discover", "-s", "tests",
             "-p", "test_history.py"], cwd=self.root, env=_child_env(),
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=300,
            preexec_fn=_isolate.DEFAULT_SIGNALS)
        log = p.stdout.decode("utf-8", "replace")
        self.assertEqual(p.returncode, 0, log)
        self.assertIn("OK", log)
        self.assertNotIn("No module named '_isolate'", log)


# ---------------------------------------------------------------------
# L-6, L-7

def _readme() -> str:
    with open(os.path.join(REPO, "README.md"), encoding="utf-8") as f:
        return f.read().replace("\r\n", "\n")


def _readme_section(heading: str) -> str:
    """The README text under a '## heading' up to the next '## '."""
    text = _readme()
    start = text.index("\n## " + heading)
    end = text.find("\n## ", start + 1)
    return text[start:end if end != -1 else len(text)]


def _run_options() -> list:
    """Every --option of `shortleash run`, from the parser itself."""
    parser = cli_mod._build_parser()
    subs = [a for a in parser._actions
            if isinstance(a, argparse._SubParsersAction)][0]
    return sorted({o for a in subs.choices["run"]._actions
                   for o in a.option_strings if o.startswith("--")})


class TestTheReadmeTellsTheTruth(unittest.TestCase):
    def test_every_option_of_run_is_in_the_readme(self):
        readme = _readme()
        options = _run_options()
        self.assertIn("--history-path", options)   # the parser was read
        self.assertIn("--fleet-ttl", options)
        # (a word boundary: `--fleet` must not pass on `--fleet-owner`)
        missing = [o for o in options if o != "--help"
                   and not re.search(re.escape(o) + r"(?![\w-])", readme)]
        self.assertEqual(missing, [])

    def test_quiet_and_history_path_are_explained_not_just_named(self):
        readme = _readme()
        self.assertRegex(readme, r"`--quiet`[^.]*\bbanner\b")
        self.assertRegex(readme, r"`--history-path (FILE|PATH)`[^.]*\bhistory\b")

    def test_quiet_really_does_what_the_readme_says(self):
        tmp = _tmp()
        self.addCleanup(_rmtree, tmp)
        rp = os.path.join(tmp, "r.json")
        hp = os.path.join(tmp, "h.jsonl")
        rc, out, err = _cli("run", "--quiet", "--receipt", rp,
                            "--history-path", hp, "--",
                            *printer("the-agent-ran"))
        self.assertEqual((rc, err), (0, ""))     # no banner, no receipt text
        self.assertIn("the-agent-ran", out)
        self.assertTrue(os.path.isfile(rp))      # the JSON receipt still is
        rc, out, err = _cli("run", "--receipt", rp, "--history-path", hp, "--",
                            *printer("the-agent-ran"))
        self.assertIn("[shortleash] guarding:", err)
        self.assertIn("shortleash receipt", err)

    def test_quiet_also_drops_the_price_notice_but_not_the_errors(self):
        # the README names the one-line notices --quiet leaves out
        tmp = _tmp()
        self.addCleanup(_rmtree, tmp)
        hp = os.path.join(tmp, "h.jsonl")
        init = json.dumps({"type": "system", "subtype": "init",
                           "model": "mystery-model-9", "session_id": "s"})
        msg = json.dumps({"type": "assistant", "message": {
            "id": "m1", "model": "mystery-model-9", "content": [],
            "usage": {"input_tokens": 10, "output_tokens": 5}}})
        rc, out, err = _cli("run", "--history-path", hp, "--",
                            *printer(init, msg))
        self.assertIn("is not in the pricing table", err)   # (not quiet)
        rc, out, err = _cli("run", "--quiet", "--history-path", hp, "--",
                            *printer(init, msg))
        self.assertEqual((rc, err), (0, ""))
        # ... while an error is still an error
        rc, out, err = _cli("run", "--quiet", "--", "no-such-program-sl7-xyz")
        self.assertEqual(rc, 2)
        self.assertIn("shortleash: error:", err)

    def test_the_pricing_line_is_where_the_readme_says(self):
        # banner (not under --quiet); receipt text when the cost shown is an
        # estimate; the JSON receipt always
        said = pricing.PricingTable(rates={}).describe()
        init = json.dumps({"type": "system", "subtype": "init",
                           "model": "claude-sonnet-4-5", "session_id": "s"})
        msg = json.dumps({"type": "assistant", "message": {
            "id": "m1", "model": "claude-sonnet-4-5", "content": [],
            "usage": {"input_tokens": 10, "output_tokens": 5}}})
        result = json.dumps({"type": "result", "subtype": "success",
                             "is_error": False, "total_cost_usd": 0.25,
                             "usage": {"input_tokens": 10,
                                       "output_tokens": 5}})
        tmp = _tmp()
        self.addCleanup(_rmtree, tmp)
        hp = os.path.join(tmp, "h.jsonl")
        for name, lines, in_text in (("estimated", (init, msg), True),
                                     ("reported", (init, msg, result), False),
                                     ("no telemetry", ("hello",), False)):
            with self.subTest(cost=name):
                rp = os.path.join(tmp, name.replace(" ", "-") + ".json")
                rc, out, err = _cli("run", "--receipt", rp, "--history-path",
                                    hp, "--", *printer(*lines))
                self.assertIn("[shortleash] pricing: " + said, err)  # banner
                receipt_text = err[err.index("shortleash receipt"):]
                self.assertEqual("  pricing:   " + said in receipt_text,
                                 in_text)
                with open(rp, encoding="utf-8") as f:
                    rec = json.load(f)
                self.assertEqual(rec["pricing"],
                                 {"as_of": pricing.PRICING_AS_OF,
                                  "source": "built-in fallback table"})
        rc, out, err = _cli("run", "--quiet", "--", *printer("hello"))
        self.assertEqual(err, "")                    # no banner under --quiet

    def test_the_readme_names_every_suffix_the_code_strips(self):
        # one list, the same wherever the README gives it (agent names are
        # recognised, and grouped in the history, without them)
        flat = " ".join(_readme().split())
        for suffix in _EXE_SUFFIXES:
            with self.subTest(suffix=suffix):
                self.assertGreaterEqual(flat.count("`%s`" % suffix), 2)
                # ... and the code does recognise claude with each of them
                for argv0 in ("claude" + suffix, "CLAUDE" + suffix.upper()):
                    self.assertEqual(agent_name(argv0), "claude")
                    argv = [argv0, "-p", "hi"]
                    self.assertEqual(cli_mod._maybe_inject_stream_json(argv),
                                     argv + STREAM_JSON)
        self.assertNotIn("`.exe`, `.cmd` or `.bat`", flat)

    def test_the_readme_does_not_overclaim_the_pricing_line(self):
        readme = _readme()
        flat = " ".join(readme.split())
        self.assertNotIn("every receipt prints", flat)
        self.assertIn("whenever the cost it shows is an estimate", flat)
        # the receipt example shows the line a real estimated run prints
        lines = readme.splitlines()
        i = next(n for n, line in enumerate(lines)
                 if line.startswith("  duration:  "))
        said = pricing.PricingTable(rates={}).describe()
        self.assertEqual(lines[i + 1], "  pricing:   " + said)

    def test_history_path_is_read_and_written(self):
        tmp = _tmp()
        self.addCleanup(_rmtree, tmp)
        hp = os.path.join(tmp, "elsewhere", "h.jsonl")
        report = json.dumps({"type": "result", "subtype": "success",
                             "is_error": False, "total_cost_usd": 0.25,
                             "usage": {"input_tokens": 10,
                                       "output_tokens": 5}})
        default = history_mod.default_history_path()   # (other tests use it)
        size = os.path.getsize(default) if os.path.exists(default) else 0
        for _ in range(2):
            rc, out, err = _cli("run", "--quiet", "--history-path", hp, "--",
                                *printer(report))
            self.assertEqual(rc, 0, err)
        with open(hp, encoding="utf-8") as f:
            self.assertEqual(len(f.read().splitlines()), 2)
        self.assertEqual(os.path.getsize(default) if os.path.exists(default)
                         else 0, size, "the default history was written to")
        # ... and the baseline is learned from it
        base = history_mod.baseline(agent_name(PY), path=hp, min_runs=2)
        self.assertEqual(base["runs"], 2)

    def test_the_echo_paragraph_quotes_no_figure_from_one_machine(self):
        readme = _readme()
        self.assertNotIn("echoed only about", readme)
        self.assertNotRegex(readme, r"\d+ MB in about a second")
        self.assertIn("bounded queue (4 MB)", readme)  # the code's own limit

    def test_the_model_limit_is_stated_and_true(self):
        # L-7: the README says the model comes from system/init only ...
        limits = _readme_section("Scope / limits")
        self.assertIn("`system`/`init` event only", limits)
        self.assertIn("total_cost_usd", limits)
        # ... and it is so: a different model named on an assistant event is
        # not read, everything is priced at the init model's rate
        init = json.dumps({"type": "system", "subtype": "init",
                           "model": "claude-haiku-4-5", "session_id": "s"})
        msg = json.dumps({"type": "assistant", "message": {
            "id": "m1", "model": "claude-opus-4-1", "content": [],
            "usage": {"input_tokens": 1000000}}})
        g = Guard(budget=1000.0, quiet=True)
        tmp = _tmp()
        self.addCleanup(_rmtree, tmp)
        g.receipt_path = os.path.join(tmp, "r.json")
        g.history_path = os.path.join(tmp, "h.jsonl")
        with redirect_stderr(io.StringIO()), redirect_stdout(io.StringIO()):
            g.run(printer(init, msg))
        with open(g.receipt_path, encoding="utf-8") as f:
            rec = json.load(f)
        self.assertEqual(rec["model"], "claude-haiku-4-5")
        self.assertAlmostEqual(rec["cost_usd"], 0.80)  # haiku, not opus's 15


class TestTheFleetExampleAndTtl(unittest.TestCase):
    def setUp(self):
        self.tmp = _tmp()
        self.addCleanup(_rmtree, self.tmp)
        patcher = mock.patch.object(fleet_mod, "DB_PATH",
                                    os.path.join(self.tmp, "fleet.db"))
        patcher.start()
        self.addCleanup(patcher.stop)

    def _claim(self, owner):
        conn = fleet_mod.connect()
        try:
            res = fleet_mod.claim(conn, [self.tmp], owner=owner,
                                  note="shortleash run: claude", ttl=1800)
        finally:
            conn.close()
        self.assertTrue(res["ok"])
        return res["claim_id"]

    def _listed(self):
        rc, out, err = _cli("fleet", "list")
        self.assertEqual((rc, err), (0, ""))
        return out.splitlines()

    def test_fleet_list_shows_the_owner_or_a_question_mark(self):
        cid = self._claim("dan")
        self.assertRegex(self._listed()[0],
                         r"^%s  .+  held by dan  \(\d+s left\)  \u2014 "
                         r"shortleash run: claude$" % cid)
        conn = fleet_mod.connect()
        try:
            fleet_mod.release(conn, cid)
        finally:
            conn.close()
        self._claim("")                       # no --fleet-owner given
        self.assertIn("  held by ?  (", self._listed()[0])

    def test_the_readme_example_gives_the_owner_it_shows(self):
        readme = _readme()
        start = readme.index("shortleash run --fleet")
        example = readme[start:readme.index("```", start)]
        self.assertIn("held by dan", example)
        self.assertIn("--fleet-owner dan", example)
        self.assertIn("held by ?", readme)         # and the default is said

    def test_ttl_outside_its_range_is_refused_before_anything_runs(self):
        for bad in ("0", "-1", "86401", "1000000000"):
            for fleet in (["--fleet"], []):        # with or without --fleet
                with self.subTest(ttl=bad, fleet=bool(fleet)):
                    rc, out, err = _cli("run", *fleet, "--fleet-ttl", bad,
                                        "--quiet", "--", *printer("ran"))
                    self.assertEqual((rc, out), (2, ""))
                    self.assertIn("--fleet-ttl must be between 1 and 86400",
                                  err)
                    self.assertIn(f"(got {bad})", err)

    def test_ttl_at_both_ends_of_its_range_is_accepted(self):
        for ok in ("1", "86400"):
            with self.subTest(ttl=ok):
                rc, out, err = _cli("run", "--fleet", "--fleet-ttl", ok,
                                    "--claim", self.tmp, "--quiet", "--",
                                    *printer("the-agent-ran"))
                self.assertEqual((rc, err), (0, ""))
                self.assertIn("the-agent-ran", out)

    def test_quiet_keeps_the_fleet_warning_and_drops_the_claim_id(self):
        # (what the README says --quiet leaves in, and out)
        self._claim("somebody-else")
        rc, out, err = _cli("run", "--fleet", "--claim", self.tmp, "--quiet",
                            "--", *printer("the-agent-ran"))
        self.assertEqual(rc, 0)
        self.assertIn("the-agent-ran", out)
        self.assertIn("fleet WARNING: paths already claimed", err)
        self.assertIn("held by somebody-else", err)
        self.assertNotIn("fleet claim ", err)        # no claim-id line

    def test_help_and_readme_state_the_range(self):
        rc, out, err = _cli("run", "--help")
        self.assertEqual(rc, 0)
        self.assertRegex(" ".join(out.split()), r"1 to 86400")
        self.assertIn("1 to 86400", _readme())


class TestTheVersionIsWrittenWhereTheReadmeSaysIt(unittest.TestCase):
    """The version lives in shortleash/__init__.py (pyproject reads it from
    there; test_round2_fixes pins that and --version). The one other place it
    is written is the README's limits heading."""

    def test_the_limits_heading_names_the_version_of_the_package(self):
        self.assertIn("\n## Scope / limits (v%s)\n" % shortleash.__version__,
                      _readme())

    def test_the_limits_say_what_this_version_was_run_on(self):
        # (a release that is not rewritten for must not inherit the claim)
        self.assertIn("**What %s was run on:**" % shortleash.__version__,
                      _readme_section("Scope / limits"))


if __name__ == "__main__":
    unittest.main()
