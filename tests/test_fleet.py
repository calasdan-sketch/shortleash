"""Fleet tests (merged from claimlock): temp DBs, real CLI runs."""

import _isolate  # noqa: F401  (isolate ~/.shortleash before importing)

import os
import sys
import tempfile
import unittest

from shortleash import cli as cli_mod
from shortleash import fleet as fleet_mod
from shortleash import paths as paths_mod
from test_cli import capture_stderr_fd

# A no-op command that exists on every platform (`true` does not on Windows).
NOOP = [sys.executable, "-c", "pass"]


def tmpdb() -> str:
    return os.path.join(tempfile.mkdtemp(), "fleet.db")


class TestFleetStore(unittest.TestCase):
    def test_claim_release(self):
        conn = fleet_mod.connect(tmpdb())
        r = fleet_mod.claim(conn, ["/tmp/fx"], owner="a", ttl=60)
        self.assertTrue(r["ok"])
        self.assertEqual(len(fleet_mod.active_claims(conn)), 1)
        self.assertEqual(fleet_mod.release(conn, r["claim_id"])["released"], 1)
        self.assertEqual(fleet_mod.active_claims(conn), [])

    def test_overlap_conflict(self):
        conn = fleet_mod.connect(tmpdb())
        fleet_mod.claim(conn, ["/tmp/fproj/src"], owner="a", ttl=60)
        r = fleet_mod.claim(conn, ["/tmp/fproj/src/api/x.py"], owner="b",
                            ttl=60)
        self.assertFalse(r["ok"])
        self.assertEqual(r["conflicts"][0]["holder"]["owner"], "a")

    def test_ttl_cap(self):
        conn = fleet_mod.connect(tmpdb())
        r = fleet_mod.claim(conn, ["/tmp/fcap"], ttl=999999999)
        self.assertTrue(r["ok"])
        self.assertLessEqual(r["expires_in"], fleet_mod.MAX_TTL)

    def test_bad_ttl(self):
        conn = fleet_mod.connect(tmpdb())
        with self.assertRaises(ValueError):
            fleet_mod.claim(conn, ["/tmp/fz"], ttl=0)


class TestFleetCLI(unittest.TestCase):
    def setUp(self):
        self._old_db = fleet_mod.DB_PATH
        self.db = tmpdb()
        fleet_mod.DB_PATH = self.db

    def tearDown(self):
        fleet_mod.DB_PATH = self._old_db

    def _run(self, *args):
        with capture_stderr_fd() as chunks:
            code = cli_mod.main(list(args))
        return code, b"".join(chunks).decode(errors="replace")

    def test_fleet_claim_and_release(self):
        d = tempfile.mkdtemp()
        code, err = self._run("run", "--fleet", "--claim", d, "--no-anomaly",
                              "--", *NOOP)
        self.assertEqual(code, 0)
        self.assertIn("fleet claim", err)
        # released after the run
        conn = fleet_mod.connect(self.db)
        self.assertEqual(fleet_mod.active_claims(conn), [])

    def test_fleet_conflict_warns_not_blocks(self):
        d = tempfile.mkdtemp()
        conn = fleet_mod.connect(self.db)
        fleet_mod.claim(conn, [d], owner="other-agent", ttl=600)
        code, err = self._run("run", "--fleet", "--claim", d, "--no-anomaly",
                              "--", *NOOP)
        self.assertEqual(code, 0)  # not blocked
        self.assertIn("WARNING", err)
        self.assertIn("other-agent", err)

    def test_fleet_default_claims_cwd(self):
        code, err = self._run("run", "--fleet", "--no-anomaly", "--", *NOOP)
        self.assertEqual(code, 0)
        self.assertIn("fleet claim", err)
        conn = fleet_mod.connect(self.db)
        self.assertEqual(fleet_mod.active_claims(conn), [])

    def test_default_db_lives_under_shortleash_home(self):
        # No DB_PATH override: the DB comes from $SHORTLEASH_HOME (which the
        # test harness points at a temp dir), never the real ~/.shortleash.
        fleet_mod.DB_PATH = None
        expected = os.path.join(os.environ["SHORTLEASH_HOME"], "fleet.db")
        self.assertEqual(paths_mod.fleet_db_path(), expected)
        conn = fleet_mod.connect()
        conn.close()
        self.assertTrue(os.path.isfile(expected))
        self.assertTrue(expected.startswith(os.environ["SHORTLEASH_HOME"]))


if __name__ == "__main__":
    unittest.main()
