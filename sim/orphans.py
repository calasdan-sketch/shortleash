#!/usr/bin/env python3
"""Fake agent that leaves processes behind in three different ways, for the
process-supervision benchmark and tests.

    python3 sim/orphans.py TAG [--spend]

Starts three `sleep` processes whose command line ends in TAG so a test can
count them: one in the agent's process group, one that called setsid() (it
left the group), and one double-forked daemon (orphaned at once). With
--spend it then emits stream-json usage forever, fast enough to trip any
--budget; otherwise it idles.
"""

import json
import os
import subprocess
import sys
import time

tag = sys.argv[1]
print(json.dumps({"type": "system", "subtype": "init",
                  "model": "claude-sonnet-5-5"}), flush=True)
sleeper = [sys.executable, "-c", "import time,sys; time.sleep(600)", tag]
subprocess.Popen(sleeper)                                  # same group
subprocess.Popen(sleeper, start_new_session=True)          # setsid
if os.fork() == 0:                                         # daemon
    os.setsid()
    if os.fork() == 0:
        os.execv(sleeper[0], sleeper)
    os._exit(0)
for i in range(100000):
    if "--spend" in sys.argv:
        print(json.dumps({"type": "assistant", "message": {
            "id": f"m{i}", "content": [],
            "usage": {"input_tokens": 100000, "output_tokens": 50000}}}),
            flush=True)
    time.sleep(0.2)
