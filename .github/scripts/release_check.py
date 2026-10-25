"""Release gate, run by .github/workflows/release.yml (standard library only).

    python .github/scripts/release_check.py ancestor
        The commit being released must be reachable from origin/main.
    python .github/scripts/release_check.py version [DIST_DIR]
        The pushed tag (GITHUB_REF_NAME, or GITHUB_REF) must be v<version>
        for every file in the build directory (default: dist). The version
        is read from the built file names, so it works whether the version is
        written in pyproject.toml or taken from the package.

Exit 0 = fine, 1 = refuse to release, 2 = the check itself could not run.
"""

import os
import re
import subprocess
import sys

_DIST_NAME = re.compile(
    r"^(?P<name>.+?)-(?P<version>\d[^-]*?)(?:\.tar\.gz|-py3[^/]*\.whl)$")


def tag_name(environ=None):
    env = os.environ if environ is None else environ
    tag = env.get("GITHUB_REF_NAME") or env.get("GITHUB_REF", "")
    prefix = "refs/tags/"
    return tag[len(prefix):] if tag.startswith(prefix) else tag


def dist_versions(directory):
    """{file name: version} for the sdist and wheel files in ``directory``."""
    found = {}
    for entry in sorted(os.listdir(directory)):
        m = _DIST_NAME.match(entry)
        if m:
            found[entry] = m.group("version")
    return found


def check_version(tag, directory):
    """Return an error message, or None when the tag matches every file."""
    if not tag:
        return "no tag name: GITHUB_REF_NAME / GITHUB_REF is not set"
    try:
        versions = dist_versions(directory)
    except OSError as e:
        return f"cannot read {directory}: {e}"
    if not versions:
        return f"no sdist or wheel found in {directory}"
    if not any(f.endswith(".tar.gz") for f in versions):
        return f"no sdist (.tar.gz) found in {directory}"
    for name, version in versions.items():
        if tag != "v" + version:
            return (f"tag {tag} does not match the built version {version} "
                    f"({name})")
    return None


def _git(*args):
    return subprocess.run(["git", *args], capture_output=True, text=True)


def check_ancestor(main_ref="origin/main"):
    """Return an error message, or None when HEAD is on the main branch."""
    remote, _, branch = main_ref.partition("/")
    fetched = _git("fetch", "--no-tags", remote, branch)
    if fetched.returncode != 0:
        return f"cannot fetch {main_ref}: {fetched.stderr.strip()}"
    head = _git("rev-parse", "HEAD")
    if head.returncode != 0:
        return f"cannot read HEAD: {head.stderr.strip()}"
    res = _git("merge-base", "--is-ancestor", "HEAD", main_ref)
    if res.returncode == 0:
        return None
    if res.returncode == 1:
        return (f"the tagged commit {head.stdout.strip()[:12]} is not on "
                f"{main_ref}: merge it to main first, then tag main")
    return f"cannot compare with {main_ref}: {res.stderr.strip()}"


def main(argv):
    if not argv or argv[0] not in ("ancestor", "version"):
        print(__doc__, file=sys.stderr)
        return 2
    if argv[0] == "version":
        problem = check_version(tag_name(), argv[1] if len(argv) > 1 else "dist")
    else:
        problem = check_ancestor(argv[1] if len(argv) > 1 else "origin/main")
    if problem:
        print(f"::error::{problem}")
        return 1
    print("release check passed:", argv[0])
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
