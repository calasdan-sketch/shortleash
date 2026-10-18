"""Test isolation: never touch the real user's ~/.shortleash.

Importing this module points SHORTLEASH_HOME (history + fleet DB) and
HOME/USERPROFILE at a fresh temp dir for the whole test process, so a
test that forgets --history-path cannot pollute the developer's
baselines. Import it before the shortleash modules in every test file
that runs the CLI or the Guard.
"""

import atexit
import os
import shutil
import signal
import tempfile

# Everything the tests create (their own temp directories, the leash's, the
# fake home) goes under one private directory that is removed when the test
# process ends, instead of piling up in the shared temp directory.
_BASE = tempfile.mkdtemp(prefix="sl-test-")
tempfile.tempdir = _BASE
for _var in ("TMPDIR", "TEMP", "TMP"):
    os.environ[_var] = _BASE
atexit.register(lambda: shutil.rmtree(_BASE, ignore_errors=True))

TEST_HOME = os.path.join(_BASE, "home")
os.makedirs(TEST_HOME)
os.environ["SHORTLEASH_HOME"] = os.path.join(TEST_HOME, ".shortleash")
os.environ["HOME"] = TEST_HOME
os.environ["USERPROFILE"] = TEST_HOME


def _default_signals():
    """Runs in a child before exec (POSIX): the signals the tests send act as
    they do for a program started from a terminal. A test runner that was
    itself started as a background job (`cmd &`) or under nohup hands
    "ignored" down for some of them, and the leash leaves an ignored signal
    alone on purpose (that is what nohup is for)."""
    for name in ("SIGINT", "SIGQUIT", "SIGHUP", "SIGTERM"):
        signal.signal(getattr(signal, name), signal.SIG_DFL)


# preexec_fn for the tests that start the leash as a process and signal it
DEFAULT_SIGNALS = _default_signals if os.name == "posix" else None
