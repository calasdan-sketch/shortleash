"""Repeat-call guard (hook mode): fingerprints, ring buffer, warn/deny,
window expiry, private state, settings sources, install, spend velocity."""

import _isolate  # noqa: F401

import contextlib
import datetime
import io
import json
import os
import stat
import tempfile
import unittest
from unittest import mock

from shortleash import cli as cli_mod
from shortleash import hookmode
from shortleash import loopguard
from shortleash import profile


def event(session="s1", tool="Bash", tool_input=None, use_id=None, cwd=None,
          transcript=None):
    ev = {"session_id": session, "tool_name": tool,
          "hook_event_name": "PreToolUse",
          "tool_input": {"command": "pytest -x"} if tool_input is None
          else tool_input}
    if use_id:
        ev["tool_use_id"] = use_id
    if cwd:
        ev["cwd"] = cwd
    if transcript:
        ev["transcript_path"] = transcript
    return ev


class Base(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="sl-loop-")
        self.state = os.path.join(self.dir, "state")
        env = {"SHORTLEASH_HOME": self.state}
        patcher = mock.patch.dict(os.environ, env)
        patcher.start()
        self.addCleanup(patcher.stop)
        for name in (loopguard.ENV_WARN, loopguard.ENV_DENY,
                     loopguard.ENV_WINDOW, loopguard.ENV_VELOCITY,
                     loopguard.ENV_OFF, hookmode.CAP_ENV):
            os.environ.pop(name, None)
        self.counter = 0

    def on(self, **cfg):
        """Switch the guard on through the saved (hook.json) settings."""
        hookmode.write_loop_config(cfg or {"warn": 4})

    def call(self, **kw):
        self.counter += 1
        kw.setdefault("use_id", f"toolu_{self.counter}")
        out, code = hookmode.run_pretool(json.dumps(event(**kw)))
        self.assertEqual(code, 0)
        return json.loads(out) if out else None


class TestFingerprint(unittest.TestCase):
    def test_same_input_same_print(self):
        a = loopguard.fingerprint("Bash", {"command": "ls", "timeout": 5})
        b = loopguard.fingerprint("Bash", {"timeout": 5, "command": "ls"})
        self.assertEqual(a, b)

    def test_white_space_is_normalised(self):
        a = loopguard.fingerprint("Bash", {"command": "pytest   -x\n"})
        b = loopguard.fingerprint("Bash", {"command": " pytest -x"})
        self.assertEqual(a, b)

    def test_different_input_or_tool_differs(self):
        base = loopguard.fingerprint("Bash", {"command": "ls"})
        self.assertNotEqual(base, loopguard.fingerprint("Bash", {"command": "ls -l"}))
        self.assertNotEqual(base, loopguard.fingerprint("Read", {"command": "ls"}))

    def test_it_holds_no_input_text(self):
        fp = loopguard.fingerprint("Bash", {"command": "echo hunter2-secret"})
        self.assertNotIn("hunter2", fp)
        self.assertTrue(fp.startswith("Bash:"))
        self.assertEqual(len(fp.split(":")[1]), 16)

    def test_hostile_inputs_are_bounded_not_fatal(self):
        deep = cur = {}
        for _ in range(5000):
            cur["a"] = {}
            cur = cur["a"]
        wide = {str(i): i for i in range(5000)}
        for value in (deep, wide, [deep] * 10, "x" * 1_000_000, None, 5,
                      float("nan"), {1: 2}, {"k": object()}):
            self.assertIsInstance(loopguard.fingerprint("T", value), str)

    def test_unicode_and_odd_keys(self):
        a = loopguard.fingerprint("Write", {"p": "caf\u00e9 \U0001F600"})
        self.assertEqual(a, loopguard.fingerprint("Write", {"p": "caf\u00e9 \U0001F600"}))


class TestSettings(Base):
    def test_off_by_default(self):
        self.assertIsNone(hookmode.resolve_loop(None))
        for _ in range(12):
            self.assertIsNone(self.call())

    def test_defaults_when_asked(self):
        cfg = loopguard.finish([{"warn": 4}])
        self.assertEqual((cfg["warn"], cfg["deny"], cfg["window"]), (4, 8, 20))

    def test_environment_wins_over_saved(self):
        self.on(warn=4, deny=8)
        env = {loopguard.ENV_WARN: "2", loopguard.ENV_DENY: "3"}
        cfg = hookmode.resolve_loop(None, env)
        self.assertEqual((cfg["warn"], cfg["deny"]), (2, 3))

    def test_env_off_wins(self):
        self.on()
        self.assertIsNone(hookmode.resolve_loop(
            None, {loopguard.ENV_OFF: "off"}))

    def test_toml_hook_table(self):
        proj = os.path.join(self.dir, "proj")
        os.makedirs(proj)
        with open(os.path.join(proj, ".shortleash.toml"), "w",
                  encoding="utf-8") as f:
            f.write("[hook]\nloop_warn = 3\nloop_deny = 5\nloop_window = 9\n"
                    "velocity_usd_per_min = 1.5\n")
        cfg = hookmode.resolve_loop(proj, {})
        self.assertEqual((cfg["warn"], cfg["deny"], cfg["window"],
                          cfg["velocity"]), (3, 5, 9, 1.5))

    def test_toml_unknown_hook_key_still_an_error(self):
        with self.assertRaises(profile.ProfileError):
            profile.validate({"hook": {"loop_warnn": 3}})
        self.assertEqual(profile.validate({"hook": {"loop_deny": 6}}),
                         {"hook": {"loop_deny": 6}})

    def test_nonsense_values_are_dropped(self):
        got = loopguard.settings_from({"warn": -1, "deny": "x", "window": True,
                                       "velocity": 0})
        self.assertEqual(got, {})
        self.assertEqual(loopguard.settings_from("no"), {})

    def test_window_never_smaller_than_the_levels(self):
        cfg = loopguard.finish([{"warn": 2, "deny": 30, "window": 5}])
        self.assertEqual(cfg["window"], 30)

    def test_install_time_checks(self):
        full = loopguard.finish([{"warn": 6, "deny": 6}])
        self.assertTrue(loopguard.problems(full))
        full = loopguard.finish([{"warn": 0, "deny": 0}])
        self.assertTrue(loopguard.problems(full))
        self.assertFalse(loopguard.problems(loopguard.finish([{"warn": 4}])))


class TestLoopDetection(Base):
    def test_a_loop_warns_then_is_refused(self):
        self.on()
        results = [self.call() for _ in range(9)]
        self.assertTrue(all(r is None for r in results[:3]))
        for r in results[3:7]:
            self.assertIn("4th" if r is results[3] else "th identical",
                          r["systemMessage"])
            self.assertNotIn("permissionDecision", r["hookSpecificOutput"])
            self.assertIn("additionalContext", r["hookSpecificOutput"])
        deny = results[7]["hookSpecificOutput"]
        self.assertEqual(deny["permissionDecision"], "deny")
        self.assertIn("identical", deny["permissionDecisionReason"])
        self.assertEqual(results[8]["hookSpecificOutput"]["permissionDecision"], "deny")

    def test_varied_calls_are_not_flagged(self):
        self.on()
        for i in range(60):
            self.assertIsNone(self.call(tool_input={"command": f"cmd {i}"}))

    def test_alternating_two_calls_stays_under_the_limit_for_a_while(self):
        self.on()
        out = [self.call(tool_input={"command": "ab"[i % 2]}) for i in range(7)]
        self.assertTrue(all(o is None or "permissionDecision" not in
                            o["hookSpecificOutput"] for o in out))

    def test_window_expiry_by_calls(self):
        self.on(warn=4, deny=8, window=10)
        for _ in range(3):
            self.call()                       # 3 identical, under the warning
        for i in range(10):                   # push them out of the window
            self.assertIsNone(self.call(tool_input={"command": f"x{i}"}))
        self.assertIsNone(self.call())        # 1 identical again, no warning

    def test_window_expiry_by_age(self):
        path = hookmode.loop_file("age")
        fp = loopguard.fingerprint("Bash", {"command": "a"})
        t0 = 1_000_000.0
        for i in range(3):
            loopguard.record_and_count(path, fp, "Bash", f"id{i}", 20, t0 + i)
        late = t0 + loopguard.MAX_AGE_SECONDS + 100
        self.assertEqual(loopguard.record_and_count(
            path, fp, "Bash", "id9", 20, late), 1)

    def test_a_retried_tool_use_id_counts_once(self):
        self.on()
        for _ in range(10):
            self.assertIsNone(self.call(use_id="toolu_same"))

    def test_sessions_are_separate(self):
        self.on()
        for _ in range(7):
            self.call(session="a")
        self.assertIsNone(self.call(session="b"))

    def test_zero_switches_a_level_off(self):
        self.on(warn=0, deny=3, window=10)
        out = [self.call() for _ in range(4)]
        self.assertIsNone(out[0])
        self.assertIsNone(out[1])
        self.assertEqual(out[2]["hookSpecificOutput"]["permissionDecision"], "deny")
        hookmode.write_loop_config({"warn": 2, "deny": 0, "window": 10})
        out = [self.call(session="z") for _ in range(5)]
        self.assertIn("systemMessage", out[1])
        self.assertTrue(all("permissionDecision" not in o["hookSpecificOutput"]
                            for o in out[1:]))

    def test_environment_can_switch_it_on_without_install(self):
        env = {loopguard.ENV_DENY: "2", loopguard.ENV_WARN: "0"}
        with mock.patch.dict(os.environ, env):
            self.assertIsNone(self.call())
            self.assertEqual(self.call()["hookSpecificOutput"]
                             ["permissionDecision"], "deny")

    def test_a_denied_spawn_does_not_use_up_the_spawn_limit(self):
        self.on(warn=0, deny=2, window=10)
        with mock.patch.dict(os.environ, {hookmode.CAP_ENV: "5"}):
            self.call(tool="Agent", tool_input={"prompt": "p"})
            denied = self.call(tool="Agent", tool_input={"prompt": "p"})
            self.assertEqual(denied["hookSpecificOutput"]["permissionDecision"], "deny")
            self.assertEqual(len(hookmode.spawn_ids(hookmode.read_state(
                hookmode.session_file("s1")))), 1)

    def test_the_spawn_limit_still_works_with_the_guard_on(self):
        self.on(warn=0, deny=50)
        with mock.patch.dict(os.environ, {hookmode.CAP_ENV: "1"}):
            self.assertIsNone(self.call(tool="Agent", tool_input={"p": 1}))
            r = self.call(tool="Agent", tool_input={"p": 2})
            self.assertIn("limit", r["hookSpecificOutput"]["permissionDecisionReason"])

    def test_without_the_guard_other_tools_are_untouched(self):
        for _ in range(20):
            self.assertIsNone(self.call(tool="Bash"))
        self.assertFalse(os.path.exists(hookmode.loop_file("s1")))


class TestFailOpen(Base):
    def test_garbage_input_is_an_allow(self):
        self.on()
        for text in ("", "not json", "[]", "null", "{}", '{"session_id": 5}',
                     '{"session_id": "s", "tool_name": 5}'):
            self.assertEqual(hookmode.run_pretool(text), ("", 0))

    def test_unwritable_state_is_an_allow(self):
        self.on()
        with mock.patch.object(loopguard.paths, "append_private",
                               side_effect=OSError("disk")):
            self.assertIsNone(self.call())

    def test_corrupt_ring_buffer_lines_are_skipped(self):
        self.on(warn=2, deny=0)
        self.call()
        path = hookmode.loop_file("s1")
        with open(path, "ab") as f:
            f.write(b"\xff\xfe not json\n{\"fp\": 5}\n[]\n")
        out = self.call()
        self.assertIn("systemMessage", out)

    def test_hostile_session_id_cannot_leave_the_folder(self):
        self.on(warn=2, deny=0)
        for sid in ("../../evil", "a/b", "C:\\x\\y", "x" * 500, "\u00e9\u0000"):
            self.call(session=sid)
            path = hookmode.loop_file(sid)
            self.assertEqual(os.path.realpath(os.path.dirname(path)),
                             os.path.realpath(hookmode.state_dir()))
            self.assertTrue(os.path.exists(path))

    def test_big_input_does_not_break_the_hook(self):
        self.on()
        self.assertIsNone(self.call(tool_input={"content": "z" * 3_000_000}))


class TestPrivateState(Base):
    @unittest.skipIf(os.name == "nt", "POSIX modes")
    def test_modes(self):
        self.on()
        self.call()
        path = hookmode.loop_file("s1")
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(os.stat(hookmode.state_dir()).st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(os.stat(hookmode.hook_config_path()).st_mode), 0o600)

    @unittest.skipIf(os.name == "nt", "POSIX modes")
    def test_a_loose_folder_is_tightened(self):
        os.makedirs(hookmode.state_dir())
        os.chmod(hookmode.state_dir(), 0o755)
        self.on()
        self.call()
        self.assertEqual(stat.S_IMODE(os.stat(hookmode.state_dir()).st_mode), 0o700)

    def test_paths_stay_under_the_state_folder_on_any_os(self):
        path = hookmode.loop_file("abc")
        self.assertTrue(path.startswith(os.path.realpath(self.state))
                        or os.path.realpath(path).startswith(os.path.realpath(self.state)))
        self.assertTrue(path.endswith(".loop"))

    def test_the_file_holds_no_command_text(self):
        self.on()
        self.call(tool_input={"command": "curl -H 'x: token-abcdef12345' h"})
        with open(hookmode.loop_file("s1"), encoding="utf-8") as f:
            text = f.read()
        self.assertNotIn("curl", text)
        self.assertNotIn("abcdef", text)

    def test_the_ring_buffer_stays_small(self):
        self.on(warn=0, deny=0, window=10)
        hookmode.write_loop_config({"warn": 3, "deny": 0, "window": 10})
        for i in range(200):
            self.call(tool_input={"command": f"c{i}"})
        with open(hookmode.loop_file("s1"), encoding="utf-8") as f:
            self.assertLess(len(f.readlines()), 10 * 4 + 20)
        self.assertFalse([n for n in os.listdir(hookmode.state_dir())
                          if n.endswith(".tmp")])

    def test_old_loop_files_are_pruned(self):
        self.on()
        self.call()
        path = hookmode.loop_file("s1")
        old = os.path.getmtime(path) - 10 * 86400
        os.utime(path, (old, old))
        self.assertEqual(hookmode.prune(), 1)
        self.assertFalse(os.path.exists(path))

    def test_status_does_not_list_ring_buffers_as_sessions(self):
        self.on()
        self.call()
        self.assertEqual(hookmode.sessions(), [])


class TestHookConfigFile(Base):
    def test_spawn_cap_and_loop_config_share_the_file(self):
        hookmode.write_default_cap(7)
        hookmode.write_loop_config({"warn": 3})
        self.assertEqual(hookmode._read_default_cap(), 7)
        hookmode.write_default_cap(None)
        self.assertIsNone(hookmode._read_default_cap())
        self.assertEqual(hookmode.resolve_loop(None, {})["warn"], 3)
        hookmode.write_loop_config(None)
        self.assertFalse(os.path.exists(hookmode.hook_config_path()))

    def test_corrupt_config_means_off(self):
        os.makedirs(os.path.dirname(hookmode.hook_config_path()))
        with open(hookmode.hook_config_path(), "w", encoding="utf-8") as f:
            f.write("{nope")
        self.assertIsNone(hookmode.resolve_loop(None, {}))


class TestInstall(Base):
    def setUp(self):
        super().setUp()
        self.settings = os.path.join(self.dir, "settings.json")
        self.base = "/venv/bin/shortleash"

    def run_cli(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli_mod.main(["hook", *args])
        return code, out.getvalue(), err.getvalue()

    def matcher(self):
        with open(self.settings, encoding="utf-8") as f:
            return json.load(f)["hooks"]["PreToolUse"][0]["matcher"]

    def test_entries_default_to_spawn_matcher_and_star_with_loop(self):
        self.assertEqual(hookmode.hook_entries(self.base)["PreToolUse"]["matcher"],
                         "Agent|Task")
        self.assertEqual(hookmode.hook_entries(self.base, True)["PreToolUse"]["matcher"],
                         "*")

    def test_install_with_loop_guard_and_back(self):
        code, out, _ = self.run_cli("install", "--settings", self.settings,
                                    "--loop-warn", "3", "--loop-deny", "6")
        self.assertEqual(code, 0, out)
        self.assertEqual(self.matcher(), "*")
        self.assertIn("Repeat-call guard", out)
        cfg = hookmode.resolve_loop(None, {})
        self.assertEqual((cfg["warn"], cfg["deny"], cfg["window"]), (3, 6, 20))
        code, out, _ = self.run_cli("install", "--settings", self.settings)
        self.assertEqual(self.matcher(), "*")       # a plain re-run leaves it
        self.assertIsNotNone(hookmode.resolve_loop(None, {}))
        code, out, _ = self.run_cli("install", "--settings", self.settings,
                                    "--no-loop-guard")
        self.assertEqual(self.matcher(), "Agent|Task")
        self.assertIsNone(hookmode.resolve_loop(None, {}))

    def test_install_twice_is_stable(self):
        args = ("install", "--settings", self.settings, "--loop-guard")
        self.run_cli(*args)
        with open(self.settings, encoding="utf-8") as f:
            first = f.read()
        self.run_cli(*args)
        with open(self.settings, encoding="utf-8") as f:
            self.assertEqual(f.read(), first)

    def test_bad_levels_are_refused_and_change_nothing(self):
        for extra in (["--loop-warn", "5", "--loop-deny", "5"],
                      ["--loop-warn", "-1"],
                      ["--velocity-usd-per-min", "0"],
                      ["--no-loop-guard", "--loop-warn", "3"]):
            code, _, err = self.run_cli("install", "--settings", self.settings,
                                        *extra)
            self.assertEqual(code, 2, extra)
            self.assertIn("error", err)
        self.assertFalse(os.path.exists(self.settings))

    def test_a_users_own_matcher_group_is_not_touched(self):
        mine = {"matcher": "Bash", "hooks": [{"type": "command",
                                              "command": "echo mine"}]}
        s = {"hooks": {"PreToolUse": [mine]}}
        hookmode.merge_hooks(s, self.base, True)
        self.assertEqual(s["hooks"]["PreToolUse"][0], mine)
        self.assertEqual(s["hooks"]["PreToolUse"][1]["matcher"], "*")

    def test_a_shared_group_keeps_its_matcher(self):
        shared = {"matcher": "Agent|Task", "hooks": [
            {"type": "command", "command": "/v/shortleash hook pretool"},
            {"type": "command", "command": "echo also mine"}]}
        s = {"hooks": {"PreToolUse": [shared]}}
        hookmode.merge_hooks(s, self.base, True)
        self.assertEqual(s["hooks"]["PreToolUse"][0]["matcher"], "Agent|Task")

    def test_dry_run_shows_the_star_matcher(self):
        code, out, _ = self.run_cli("install", "--dry-run", "--loop-guard",
                                    "--settings", self.settings)
        self.assertIn('"matcher": "*"', out)
        self.assertFalse(os.path.exists(self.settings))

    def test_status_reports_the_guard(self):
        code, out, _ = self.run_cli("status")
        self.assertIn("repeat-call guard: off", out)
        hookmode.write_loop_config({"warn": 4})
        code, out, _ = self.run_cli("status")
        self.assertIn("warn at 4, refuse at 8", out)

    def test_init_still_works(self):
        proj = os.path.join(self.dir, "p")
        os.makedirs(proj)
        out, err = io.StringIO(), io.StringIO()
        cwd = os.getcwd()
        try:
            os.chdir(proj)
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code = cli_mod.main(["init", "--settings", self.settings])
        finally:
            os.chdir(cwd)
        self.assertEqual(code, 0, err.getvalue())


def transcript_line(ts, usage, mid, model="claude-sonnet-4-5"):
    return json.dumps({"type": "assistant", "timestamp": ts,
                       "message": {"id": mid, "model": model, "usage": usage}})


def iso(epoch):
    return datetime.datetime.fromtimestamp(
        epoch, datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


class TestVelocity(Base):
    NOW = 1_800_000_000.0

    def transcript(self, lines, raw=None):
        path = os.path.join(self.dir, "t.jsonl")
        with open(path, "wb") as f:
            f.write(raw if raw is not None else
                    ("\n".join(lines) + "\n").encode("utf-8"))
        return path

    def test_rate_from_recent_messages(self):
        big = {"input_tokens": 1_000_000, "output_tokens": 0}
        path = self.transcript([transcript_line(iso(self.NOW - 60), big, "m1")])
        rate = loopguard.spend_rate(path, self.NOW)
        self.assertIsNotNone(rate)
        self.assertGreater(rate, 0.1)

    def test_old_messages_and_missing_data_give_none(self):
        usage = {"input_tokens": 100}
        old = self.transcript([transcript_line(iso(self.NOW - 4000), usage, "m")])
        self.assertIsNone(loopguard.spend_rate(old, self.NOW))
        self.assertIsNone(loopguard.spend_rate(os.path.join(self.dir, "no"), self.NOW))
        self.assertIsNone(loopguard.spend_rate(None, self.NOW))
        self.assertIsNone(loopguard.spend_rate(5, self.NOW))

    def test_duplicate_message_ids_count_once(self):
        usage = {"input_tokens": 1_000_000}
        one = self.transcript([transcript_line(iso(self.NOW - 30), usage, "m")])
        r1 = loopguard.spend_rate(one, self.NOW)
        two = self.transcript([transcript_line(iso(self.NOW - 30), usage, "m")] * 3)
        self.assertEqual(loopguard.spend_rate(two, self.NOW), r1)

    def test_garbage_lines_and_crlf_are_skipped(self):
        usage = {"input_tokens": 1_000_000}
        good = transcript_line(iso(self.NOW - 30), usage, "m")
        raw = ("not json\r\n[1]\r\n{\"type\":\"assistant\"}\r\n"
               + good + "\r\n\xff\r\n").encode("utf-8", "surrogateescape")
        path = self.transcript(None, raw=raw)
        self.assertIsNotNone(loopguard.spend_rate(path, self.NOW))

    def test_only_the_tail_is_read_and_a_cut_first_line_is_ignored(self):
        usage = {"input_tokens": 1_000_000}
        filler = "x" * 2_000_000
        raw = (filler + "\n" + transcript_line(
            iso(self.NOW - 10), usage, "m") + "\n").encode()
        path = self.transcript(None, raw=raw)
        self.assertIsNotNone(loopguard.spend_rate(path, self.NOW))

    def test_a_huge_line_is_not_fatal(self):
        path = self.transcript(None, raw=b"{" + b"1" * 5_000_000 + b"\n")
        self.assertIsNone(loopguard.spend_rate(path, self.NOW))

    def test_hook_warns_once_above_the_limit(self):
        hookmode.write_loop_config({"warn": 0, "deny": 0, "velocity": 0.01})
        usage = {"input_tokens": 5_000_000}
        import time as real_time
        now = real_time.time()
        path = self.transcript([transcript_line(iso(now - 20), usage, "m")])
        first = self.call(tool_input={"a": 1}, transcript=path)
        self.assertIn("per minute", first["systemMessage"])
        self.assertNotIn("permissionDecision", first["hookSpecificOutput"])
        second = self.call(tool_input={"a": 2}, transcript=path)
        self.assertIsNone(second)           # not again within ten minutes

    def test_hook_is_silent_below_the_limit_or_without_data(self):
        hookmode.write_loop_config({"warn": 0, "deny": 0, "velocity": 1000})
        import time as real_time
        path = self.transcript([transcript_line(
            iso(real_time.time() - 20), {"input_tokens": 10}, "m")])
        self.assertIsNone(self.call(transcript=path))
        self.assertIsNone(self.call(tool_input={"b": 1}))      # no transcript

    def test_velocity_due_after_ten_minutes(self):
        path = hookmode.loop_file("v")
        loopguard.note_velocity(path, 1000.0)
        self.assertFalse(loopguard.velocity_due(path, 1000.0 + 100))
        self.assertTrue(loopguard.velocity_due(path, 1000.0 + 700))


class TestThePieces(unittest.TestCase):
    def test_level(self):
        cfg = {"warn": 4, "deny": 8}
        self.assertEqual([loopguard.level(n, cfg) for n in (1, 3, 4, 7, 8, 30)],
                         ["ok", "ok", "warn", "warn", "deny", "deny"])

    def test_texts_name_the_tool_and_the_numbers(self):
        cfg = {"warn": 4, "deny": 8, "window": 20}
        self.assertIn("5th identical Bash", loopguard.warn_text(5, "Bash", cfg))
        self.assertIn("8 times", loopguard.deny_text(8, "Bash", cfg))
        self.assertIn("$2.00 per minute", loopguard.velocity_text(2.0, 1.0))


if __name__ == "__main__":
    unittest.main()
