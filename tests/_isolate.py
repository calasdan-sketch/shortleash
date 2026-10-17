"""Test isolation: never touch the real user's ~/.shortleash.

Importing this module points SHORTLEASH_HOME (history + fleet DB) and
HOME/USERPROFILE at a fresh temp dir for the whole test process, so a
test that forgets --history-path cannot pollute the developer's
baselines. Import it before the shortleash modules in every test file
that runs the CLI or the Guard.
"""

import os
import tempfile

TEST_HOME = tempfile.mkdtemp(prefix="sl-home-")
os.environ["SHORTLEASH_HOME"] = os.path.join(TEST_HOME, ".shortleash")
os.environ["HOME"] = TEST_HOME
os.environ["USERPROFILE"] = TEST_HOME
