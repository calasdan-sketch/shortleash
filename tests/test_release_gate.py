"""Release gate: the workflow files and .github/scripts/release_check.py."""

import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(ROOT, ".github", "scripts", "release_check.py")
WORKFLOWS = os.path.join(ROOT, ".github", "workflows")
NAME = "shortleash"

needs_checkout = unittest.skipUnless(
    os.path.isfile(SCRIPT), "not in a git checkout (an sdist has no .github)")


def _run(args, env=None, cwd=None):
    full = dict(os.environ)
    for k in ("GITHUB_REF", "GITHUB_REF_NAME"):
        full.pop(k, None)
    full.update(env or {})
    return subprocess.run([sys.executable, "-I", SCRIPT, *args],
                          capture_output=True, text=True, env=full, cwd=cwd)


def _package_version():
    with open(os.path.join(ROOT, "shortleash", "__init__.py"),
              encoding="utf-8") as f:
        return re.search(r'__version__\s*=\s*"([^"]+)"', f.read()).group(1)


def _git(cwd, *args):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True,
                   env={**os.environ, "GIT_AUTHOR_NAME": "t",
                        "GIT_AUTHOR_EMAIL": "t@example.com",
                        "GIT_COMMITTER_NAME": "t",
                        "GIT_COMMITTER_EMAIL": "t@example.com"})


@needs_checkout
class TestVersionCheck(unittest.TestCase):
    def setUp(self):
        self.dist = tempfile.mkdtemp(prefix="rel-")
        self.addCleanup(shutil.rmtree, self.dist, True)

    def _touch(self, *names):
        for n in names:
            open(os.path.join(self.dist, n), "w").close()

    def test_matching_tag_passes(self):
        self._touch(f"{NAME}-1.2.3.tar.gz", f"{NAME}-1.2.3-py3-none-any.whl")
        r = _run(["version", self.dist], {"GITHUB_REF_NAME": "v1.2.3"})
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

    def test_wrong_tag_refused(self):
        self._touch(f"{NAME}-1.2.3.tar.gz", f"{NAME}-1.2.3-py3-none-any.whl")
        r = _run(["version", self.dist], {"GITHUB_REF_NAME": "v1.2.4"})
        self.assertEqual(r.returncode, 1)
        self.assertIn("does not match", r.stdout)

    def test_tag_from_full_ref(self):
        self._touch(f"{NAME}-1.2.3.tar.gz")
        r = _run(["version", self.dist], {"GITHUB_REF": "refs/tags/v1.2.3"})
        self.assertEqual(r.returncode, 0, r.stdout)

    def test_missing_tag_refused(self):
        self._touch(f"{NAME}-1.2.3.tar.gz")
        self.assertEqual(_run(["version", self.dist]).returncode, 1)

    def test_empty_dist_refused(self):
        r = _run(["version", self.dist], {"GITHUB_REF_NAME": "v1.2.3"})
        self.assertEqual(r.returncode, 1)

    def test_wheel_only_refused(self):
        self._touch(f"{NAME}-1.2.3-py3-none-any.whl")
        r = _run(["version", self.dist], {"GITHUB_REF_NAME": "v1.2.3"})
        self.assertEqual(r.returncode, 1)

    def test_one_odd_file_refused(self):
        self._touch(f"{NAME}-1.2.3.tar.gz", f"{NAME}-1.2.2-py3-none-any.whl")
        r = _run(["version", self.dist], {"GITHUB_REF_NAME": "v1.2.3"})
        self.assertEqual(r.returncode, 1)

    def test_unknown_command_is_a_usage_error(self):
        self.assertEqual(_run(["frobnicate"]).returncode, 2)

    def test_current_version_has_no_pyproject_version_key_needed(self):
        # The package's own version, as the built file names would carry it.
        v = _package_version()
        self._touch(f"{NAME}-{v}.tar.gz")
        r = _run(["version", self.dist], {"GITHUB_REF_NAME": "v" + v})
        self.assertEqual(r.returncode, 0, r.stdout)


@needs_checkout
@unittest.skipUnless(shutil.which("git"), "needs git")
class TestAncestorCheck(unittest.TestCase):
    def setUp(self):
        base = tempfile.mkdtemp(prefix="anc-")
        self.addCleanup(shutil.rmtree, base, True)
        self.origin = os.path.join(base, "origin")
        self.work = os.path.join(base, "work")
        subprocess.run(["git", "init", "-q", "-b", "main", self.origin],
                       check=True)
        _git(self.origin, "commit", "-q", "--allow-empty", "-m", "one")
        subprocess.run(["git", "clone", "-q", self.origin, self.work],
                       check=True)

    def test_commit_on_main_passes(self):
        r = _run(["ancestor"], cwd=self.work)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

    def test_older_commit_of_main_passes(self):
        _git(self.origin, "commit", "-q", "--allow-empty", "-m", "two")
        r = _run(["ancestor"], cwd=self.work)   # work is one behind main
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

    def test_commit_only_on_a_branch_is_refused(self):
        _git(self.work, "checkout", "-q", "-b", "feature")
        _git(self.work, "commit", "-q", "--allow-empty", "-m", "side")
        r = _run(["ancestor"], cwd=self.work)
        self.assertEqual(r.returncode, 1)
        self.assertIn("not on origin/main", r.stdout)

    def test_no_origin_is_refused(self):
        _git(self.work, "remote", "remove", "origin")
        self.assertEqual(_run(["ancestor"], cwd=self.work).returncode, 1)


@needs_checkout
class TestWorkflowFiles(unittest.TestCase):
    def _text(self, name):
        with open(os.path.join(WORKFLOWS, name), encoding="utf-8") as f:
            return f.read()

    def test_no_multi_line_run_is_a_plain_scalar(self):
        # `run: python - <<'PY'` followed by indented lines is ONE folded line
        # in YAML: the script never reaches the interpreter. Every run that is
        # not a single line must be a `|` block.
        for name in ("release.yml", "ci.yml"):
            lines = self._text(name).split("\n")
            for i, line in enumerate(lines):
                m = re.match(r"^(\s*)(?:- )?run:\s*(\S.*)$", line)
                if m and not m.group(2).startswith(("|", ">")):
                    nxt = lines[i + 1] if i + 1 < len(lines) else ""
                    indent = len(m.group(1))
                    self.assertFalse(
                        nxt.strip() and len(nxt) - len(nxt.lstrip()) > indent + 2,
                        f"{name}:{i + 1} is a plain scalar that continues")
                    self.assertNotIn("<<", m.group(2), f"{name}:{i + 1}")

    def test_release_checks_ancestry_and_version(self):
        text = self._text("release.yml")
        self.assertIn("release_check.py ancestor", text)
        self.assertIn("release_check.py version", text)
        self.assertIn("fetch-depth: 0", text)
        self.assertLess(text.index("release_check.py ancestor"),
                        text.index("python -m build"))
        self.assertLess(text.index("python -m build"),
                        text.index("release_check.py version"))

    def test_release_does_not_read_a_version_from_pyproject(self):
        self.assertNotIn("tomllib", self._text("release.yml"))

    def test_ci_runs_the_tests_of_the_sdist(self):
        text = self._text("ci.yml")
        self.assertIn("tar -xzf dist/*.tar.gz", text)
        self.assertRegex(text, r"unpacked sdist[\s\S]*unittest discover")


if __name__ == "__main__":
    unittest.main()
