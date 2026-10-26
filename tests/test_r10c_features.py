"""Round 10c: private state, single-write history, price staleness, hook
state pruning, `init` and `report`."""

import datetime
import io
import json
import os
import stat
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

from shortleash import cli as cli_mod
from shortleash import costreport, history, hookmode, paths, pricing, profile


def run(*argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = cli_mod.main(list(argv))
    return code, out.getvalue(), err.getvalue()


class Home(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp(prefix="slh")
        p = mock.patch.dict(os.environ, {"SHORTLEASH_HOME":
                                         os.path.join(self.home, "state")})
        p.start()
        self.addCleanup(p.stop)


@unittest.skipUnless(os.name == "posix", "POSIX modes")
class TestPrivateState(Home):
    def mode(self, p):
        return stat.S_IMODE(os.stat(p).st_mode)

    def test_hook_state_dirs_are_0700_and_files_0600(self):
        with mock.patch.object(os, "umask", wraps=os.umask):
            old = os.umask(0o000)       # a lax umask must not widen anything
            try:
                hookmode._append(hookmode.session_file("abc"), {"t": "x"})
            finally:
                os.umask(old)
        self.assertEqual(self.mode(paths.state_dir()), 0o700)
        self.assertEqual(self.mode(hookmode.state_dir()), 0o700)
        self.assertEqual(self.mode(hookmode.session_file("abc")), 0o600)

    def test_an_existing_loose_dir_that_is_ours_is_tightened(self):
        os.makedirs(paths.state_dir(), mode=0o755)
        os.chmod(paths.state_dir(), 0o755)
        paths.private_dir(paths.state_dir())
        self.assertEqual(self.mode(paths.state_dir()), 0o700)

    def test_history_is_0600_and_written_in_one_os_write(self):
        path = paths.history_path()
        calls = []
        real = os.write
        with mock.patch("shortleash.paths.os.write",
                        side_effect=lambda fd, b: (calls.append(b), real(fd, b))[1]):
            history.record({"agent": "claude", "cost_usd": 1.5,
                            "tokens": {"input": 10}, "ended_at": "x"}, path)
        self.assertEqual(len(calls), 1)
        self.assertTrue(calls[0].endswith(b"\n"))
        self.assertEqual(self.mode(path), 0o600)
        self.assertEqual(self.mode(os.path.dirname(path)), 0o700)


class TestHookStatePruning(Home):
    def test_files_older_than_seven_days_go_when_a_session_starts(self):
        self.assertEqual(hookmode.KEEP_DAYS, 7)
        old = hookmode.session_file("old")
        hookmode._append(old, {"t": "x"})
        t = time.time() - 8 * 86400
        os.utime(old, (t, t))
        recent = hookmode.session_file("recent")
        hookmode._append(recent, {"t": "x"})
        t = time.time() - 6 * 86400
        os.utime(recent, (t, t))
        hookmode._append(hookmode.session_file("new"), {"t": "x"})
        self.assertFalse(os.path.exists(old))
        self.assertTrue(os.path.exists(recent))


class TestPricing(unittest.TestCase):
    def test_every_price_key_resolves_to_itself_and_is_sane(self):
        for key, rate in pricing.DEFAULT_PRICING.items():
            self.assertEqual(len(rate), 2, key)
            self.assertTrue(all(isinstance(x, float) or isinstance(x, int)
                                and x >= 0 for x in rate), key)
            if key != "default":
                self.assertEqual(pricing.match(key)[0], key, key)

    def test_the_table_date_parses(self):
        datetime.date.fromisoformat(pricing.PRICING_AS_OF)

    def test_stale_after_ninety_days(self):
        d = datetime.date.fromisoformat("2026-10-24")
        self.assertIsNone(pricing.staleness("2026-10-24", d))
        self.assertIsNone(pricing.staleness("2026-10-24", d + datetime.timedelta(days=90)))
        msg = pricing.staleness("2026-10-24", d + datetime.timedelta(days=91))
        self.assertIn("91 days old", msg)
        self.assertIn("no usable date", pricing.staleness("unknown (no as_of in file)", d))

    def test_run_warns_on_stderr_when_the_table_is_old(self):
        class Stop(Exception):
            pass
        with mock.patch.object(pricing, "staleness", return_value="OLD TABLE"), \
                mock.patch.object(cli_mod.guard_mod.Guard, "run",
                                  return_value=0):
            code, out, err = run("run", "--budget", "1", "--no-config",
                                 "--", sys.executable, "-c", "pass")
        self.assertEqual(code, 0)
        self.assertIn("WARNING: OLD TABLE", err)


class TestInitAndReport(Home):
    def setUp(self):
        super().setUp()
        self.cwd = tempfile.mkdtemp(prefix="slc")
        self.settings = os.path.join(self.cwd, "settings.json")
        old = os.getcwd()
        os.chdir(self.cwd)
        self.addCleanup(os.chdir, old)

    def test_init_installs_the_hook_and_writes_a_profile(self):
        code, out, err = run("init", "--settings", self.settings)
        self.assertEqual(code, 0, err)
        data = profile.load(os.path.join(os.path.realpath(self.cwd), ".shortleash.toml"))
        self.assertEqual((data["budget"], data["max_spawns"], data["timeout"]),
                         (5.0, 10, 1800))
        self.assertTrue(hookmode.installed_in(self.settings))
        # twice: nothing is overwritten
        with open(".shortleash.toml", "w", encoding="utf-8") as f:
            f.write("budget = 1\n")
        code, out, _ = run("init", "--settings", self.settings)
        self.assertEqual(code, 0)
        self.assertIn("Kept the profile", out)
        self.assertEqual(profile.load(".shortleash.toml")["budget"], 1)

    def test_dry_run_changes_nothing(self):
        code, out, _ = run("init", "--dry-run", "--settings", self.settings)
        self.assertEqual(code, 0)
        self.assertFalse(os.path.exists(self.settings))
        self.assertFalse(os.path.exists(".shortleash.toml"))

    def test_report_sums_the_window(self):
        now = datetime.datetime(2026, 10, 26, 12, tzinfo=datetime.timezone.utc)
        rows = [("claude", 2.0, 2), ("claude", 3.0, 3), ("aider", 1.0, 4),
                ("claude", 50.0, 30)]
        p = os.path.join(self.cwd, "h.jsonl")
        with open(p, "w", encoding="utf-8") as f:
            for agent, cost, ago in rows:
                at = (now - datetime.timedelta(days=ago)).isoformat()
                f.write(json.dumps({"agent": agent, "profile": "", "cost_usd": cost,
                                    "tokens": 100, "breached": agent == "aider",
                                    "ended_at": at}) + "\n")
            f.write("not json\n")
            f.write(json.dumps({"agent": "x", "cost_usd": 1, "tokens": 1,
                                "ended_at": "nonsense"}) + "\n")
        r = costreport.summarize(p, 7, now)
        self.assertEqual((r["runs"], round(r["cost_usd"], 2), r["breached"]), (3, 6.0, 1))
        self.assertEqual(r["skipped"], 2)
        self.assertEqual(r["by_key"]["claude"]["runs"], 2)
        text = "\n".join(costreport.to_text(r))
        self.assertIn("3 guarded run(s), $6.00", text)

    def test_report_command_and_bad_days(self):
        code, out, _ = run("report", "--history-path",
                           os.path.join(self.cwd, "none.jsonl"), "--json")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["runs"], 0)
        self.assertEqual(run("report", "--days", "0")[0], 2)


if __name__ == "__main__":
    unittest.main()
