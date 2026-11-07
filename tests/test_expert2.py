"""0.3.0: the hook's incremental session tally, the spend ledger and its
daily / project caps, the tamper-evident receipt chain, prompt blocking,
fuzzy loop matching, the error-streak cap and the process sentinel."""

import _isolate  # noqa: F401  (isolate ~/.shortleash before importing)

import io
import json
import multiprocessing
import os
import random
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stderr

from shortleash import chain, cli as cli_mod, events, ledger, loops
from shortleash import hook as hook_mod
from shortleash import proctree

PY = sys.executable
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ORPHANS = os.path.join(REPO, "sim", "orphans.py")


def tmpdir() -> str:
    return tempfile.mkdtemp(prefix="sl-e2-")


def amsg(mid, usage=None, model="claude-sonnet-4-5", tools=(), text=False):
    content = [{"type": "tool_use", "id": t, "name": n, "input": i}
               for t, n, i in tools]
    if text:
        content.insert(0, {"type": "text", "text": "hi"})
    return {"type": "assistant", "message": {
        "id": mid, "model": model, "content": content,
        "usage": usage or {"input_tokens": 1000, "output_tokens": 100}}}


def umsg(*results):
    """A user line with tool_result blocks: (tool_use_id, content, is_error)."""
    return {"type": "user", "message": {"content": [
        {"type": "tool_result", "tool_use_id": t, "content": c,
         **({"is_error": True} if e else {})} for t, c, e in results]}}


def put(path, *entries, mode="a", newline=True):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, mode, encoding="utf-8") as f:
        for e in entries:
            f.write(e if isinstance(e, str) else json.dumps(e))
            if newline:
                f.write("\n")


def snapshot(t: "hook_mod.SessionTally") -> dict:
    return {"usage": t.usage_by_model, "spawns": sorted(t.spawn_ids),
            "reported": t.reported_cost, "bad": t.bad_lines,
            "calls": {os.path.basename(p): [tuple(c) for c in v]
                      for p, v in t.calls.items()},
            "streaks": {os.path.basename(p): v for p, v in t.streaks.items()}}


class HookHarness(unittest.TestCase):
    def setUp(self):
        self.dir = tmpdir()
        self.home = os.path.join(self.dir, "home")
        self._old = os.environ.get("SHORTLEASH_HOME")
        os.environ["SHORTLEASH_HOME"] = self.home
        self.proj = os.path.join(self.dir, "proj-a")
        self.t = os.path.join(self.proj, "sess.jsonl")
        self.sub = os.path.join(self.proj, "sess", "subagents", "agent-1.jsonl")

    def tearDown(self):
        if self._old is None:
            os.environ.pop("SHORTLEASH_HOME", None)
        else:
            os.environ["SHORTLEASH_HOME"] = self._old
        shutil.rmtree(self.dir, ignore_errors=True)

    def event(self, **kw):
        e = {"session_id": "sess", "transcript_path": self.t,
             "hook_event_name": "PreToolUse", "tool_name": "Bash",
             "tool_input": {"command": "ls"}, "tool_use_id": "toolu_new"}
        e.update(kw)
        return e

    def hook(self, event, *argv):
        args = cli_mod._build_parser().parse_args(["hook", *argv])
        out, err = io.StringIO(), io.StringIO()
        stdin = io.StringIO(event if isinstance(event, str) else json.dumps(event))
        code = hook_mod.run_hook(args, stdin=stdin, stdout=out, stderr=err)
        text = out.getvalue()
        return code, (json.loads(text) if text.strip() else None), err.getvalue()


# -- the incremental tally ----------------------------------------------


class TestIncrementalTally(HookHarness):
    def both(self, fuzzy=False):
        a = hook_mod.tally_session(self.t, use_cache=False, fuzzy=fuzzy)
        b = hook_mod.tally_session(self.t, use_cache=True, fuzzy=fuzzy)
        self.assertEqual(snapshot(a), snapshot(b))
        return b

    def test_cached_equals_full_read_over_random_growth(self):
        rng = random.Random(7)
        mids = 0
        pending = []
        for step in range(60):
            lines = []
            for _ in range(rng.randint(1, 4)):
                kind = rng.random()
                if kind < 0.5:
                    mids += 1
                    tid = f"toolu_{mids}"
                    pending.append(tid)
                    m = amsg(f"m{mids}", {"input_tokens": rng.randint(1, 900),
                                          "output_tokens": rng.randint(1, 90),
                                          "cache_read_input_tokens": rng.randint(0, 5000)},
                             model=rng.choice(["claude-sonnet-5-5", "claude-opus-5-5"]),
                             tools=[(tid, rng.choice(["Bash", "Agent", "Read"]),
                                     {"command": f"c{rng.randint(0, 3)}"})])
                    lines.append(m)
                    if rng.random() < 0.3:
                        lines.append(m)  # the copy of a multi-block message
                elif kind < 0.8 and pending:
                    tid = pending.pop(rng.randrange(len(pending)))
                    lines.append(umsg((tid, f"out{rng.randint(0, 2)}", rng.random() < 0.3)))
                elif kind < 0.9:
                    lines.append({"type": "cost-state", "totalCostUSD": step * 0.01})
                else:
                    lines.append("not json at all")
            put(self.t, *lines)
            if step % 7 == 0:
                put(self.sub, amsg(f"s{step}", {"output_tokens": 77},
                                   tools=[(f"st{step}", "Bash", {"command": "x"})]))
            self.both()

    def test_half_written_last_line_is_not_counted_twice(self):
        put(self.t, amsg("m1", {"input_tokens": 1000}))
        line = json.dumps(amsg("m2", {"input_tokens": 2000}))
        put(self.t, line, newline=False)  # complete JSON, newline not landed
        t1 = self.both()
        self.assertEqual(t1.tokens(), 3000)
        put(self.t, "")  # the newline lands
        t2 = self.both()
        self.assertEqual(t2.tokens(), 3000)
        put(self.t, '{"type":"assistant","message":{"id":"m3"', newline=False)
        t3 = self.both()  # a torn line: skipped, never a crash
        self.assertEqual(t3.tokens(), 3000)

    def test_a_replaced_transcript_is_read_again(self):
        put(self.t, amsg("m1", {"input_tokens": 5000}))
        self.assertEqual(self.both().tokens(), 5000)
        put(self.t, amsg("z9", {"input_tokens": 7000}), mode="w")
        self.assertEqual(self.both().tokens(), 7000)
        # same length, different content, later mtime
        put(self.t, amsg("y9", {"input_tokens": 7001}), mode="w")
        os.utime(self.t, (time.time() + 5, time.time() + 5))
        self.assertEqual(self.both().tokens(), 7001)

    def test_a_truncated_transcript_is_read_again(self):
        put(self.t, *[amsg(f"m{i}", {"input_tokens": 100}) for i in range(10)])
        self.assertEqual(self.both().tokens(), 1000)
        put(self.t, amsg("m0", {"input_tokens": 100}), mode="w")
        self.assertEqual(self.both().tokens(), 100)

    def test_corrupt_or_foreign_cache_files_are_ignored(self):
        put(self.t, amsg("m1", {"input_tokens": 4000}))
        hook_mod.tally_session(self.t, use_cache=True)
        d = hook_mod._cache_dir(self.t)
        files = [os.path.join(d, n) for n in os.listdir(d)]
        self.assertTrue(files)
        for garbage in ("{", "[]", '{"v": 99}', '{"v":1,"path":"x"}',
                        json.dumps({"v": 1, "path": self.t, "head": "", "size": 1,
                                    "mtime_ns": 1, "state": {"offset": -5}}),
                        "\x00" * 50):
            for f in files:
                with open(f, "w", encoding="utf-8") as fh:
                    fh.write(garbage)
            self.assertEqual(self.both().tokens(), 4000, garbage[:30])

    def test_an_unwritable_state_directory_only_costs_speed(self):
        put(self.t, amsg("m1", {"input_tokens": 4000}))
        blocker = os.path.join(self.dir, "file-not-dir")
        with open(blocker, "w", encoding="utf-8"):
            pass
        os.environ["SHORTLEASH_HOME"] = blocker  # cannot hold a directory
        self.assertEqual(hook_mod.tally_session(self.t, use_cache=True).tokens(), 4000)

    def test_an_absurdly_long_line_is_skipped_not_held(self):
        old = hook_mod._MAX_LINE
        hook_mod._MAX_LINE = 1000
        try:
            put(self.t, amsg("m1", {"input_tokens": 10}))
            put(self.t, '{"type":"assistant","x":"' + "a" * 5000 + '"}')
            put(self.t, amsg("m2", {"input_tokens": 20}))
            t = self.both()
            self.assertEqual(t.tokens(), 30)
            self.assertEqual(t.bad_lines, 1)
        finally:
            hook_mod._MAX_LINE = old

    def test_the_cache_is_kept_per_fuzzy_mode(self):
        put(self.t, amsg("m1", tools=[("t1", "Bash", {"command": "sleep 5"})]))
        a = self.both(False)
        b = self.both(True)
        self.assertNotEqual(a.calls[self.t][0][1], b.calls[self.t][0][1])

    def test_loops_still_work_on_a_cached_session(self):
        for i in range(3):
            put(self.t, amsg(f"m{i}", tools=[(f"t{i}", "Bash", {"command": "pytest"})]),
                umsg((f"t{i}", "FAILED", True)))
            self.hook(self.event(tool_use_id=f"x{i}", tool_input={"command": "ls"}),
                      "--max-repeats", "4")
        code, out, _ = self.hook(self.event(tool_input={"command": "pytest"}),
                                 "--max-repeats", "4")
        self.assertIn("loop detected", out["hookSpecificOutput"]["permissionDecisionReason"])

    def test_the_second_call_reads_only_what_was_appended(self):
        put(self.t, *[amsg(f"m{i}", {"input_tokens": 10}) for i in range(50)])
        hook_mod.tally_session(self.t, use_cache=True)
        reads = []
        real = hook_mod.FileState.feed_line

        def spy(st, line, main):
            reads.append(line)
            return real(st, line, main)

        hook_mod.FileState.feed_line = spy
        try:
            put(self.t, amsg("new", {"input_tokens": 10}))
            t = hook_mod.tally_session(self.t, use_cache=True)
        finally:
            hook_mod.FileState.feed_line = real
        self.assertEqual(len(reads), 1)
        self.assertEqual(t.tokens(), 510)


# -- the ledger and the daily caps ---------------------------------------


def _writer(args):
    home, ident, n = args
    os.environ["SHORTLEASH_HOME"] = home
    for i in range(1, n + 1):
        ledger.record(ident, i * 0.01, "p")
    return True


class TestLedger(HookHarness):
    def test_records_only_growth_and_sums_ids(self):
        self.assertTrue(ledger.record("a", 1.0, "p"))
        self.assertFalse(ledger.record("a", 1.001, "p"))  # below the step
        self.assertFalse(ledger.record("a", 0.5, "p"))    # never goes down
        self.assertTrue(ledger.record("a", 2.0, "p"))
        ledger.record("b", 0.5, "q")
        self.assertAlmostEqual(ledger.today_spend(), 2.5)
        self.assertAlmostEqual(ledger.today_spend(project="p"), 2.0)
        self.assertAlmostEqual(ledger.today_spend(exclude="a"), 0.5)

    def test_a_session_across_midnight_is_split(self):
        day1 = time.mktime((2026, 11, 3, 23, 0, 0, 0, 0, -1))
        day2 = time.mktime((2026, 11, 4, 1, 0, 0, 0, 0, -1))
        ledger.record("s", 3.0, "p", now=day1)
        ledger.record("s", 8.0, "p", now=day2)
        self.assertAlmostEqual(ledger.today_spend(now=day1 + 60), 3.0)
        self.assertAlmostEqual(ledger.today_spend(now=day2 + 60), 5.0)

    def test_unsafe_ids_and_bad_figures(self):
        for bad in (float("nan"), float("inf"), -1.0, 1e13, True):
            self.assertFalse(ledger.record("x", bad, "p"))
        ledger.record("../../etc/passwd\n", 1.0, "p\nq")
        names = os.listdir(ledger.ledger_dir())
        self.assertEqual(len(names), 1)
        self.assertRegex(names[0], r"^[0-9a-f]{32}\.jsonl$")

    def test_a_corrupt_line_is_skipped(self):
        ledger.record("a", 1.0, "p")
        path = os.path.join(ledger.ledger_dir(), ledger.run_id("a") + ".jsonl")
        with open(path, "ab") as f:
            f.write(b'{"t": 1, "c": \n\xff\xfe\n{"t":"x","c":9}\n[]\n')
        self.assertAlmostEqual(ledger.today_spend(), 1.0)
        self.assertTrue(ledger.record("a", 2.0, "p"))  # still appendable

    def test_concurrent_writers_lose_nothing(self):
        n_proc, n = 6, 40
        ctx = multiprocessing.get_context("spawn" if os.name == "nt" else "fork")
        with ctx.Pool(n_proc) as pool:
            pool.map(_writer, [(self.home, f"id{i}", n) for i in range(n_proc)])
        self.assertAlmostEqual(ledger.today_spend(), n_proc * n * 0.01, places=4)

    def test_the_hook_denies_over_the_daily_budget_across_sessions(self):
        other = os.path.join(self.dir, "proj-b", "other.jsonl")
        put(other, amsg("o1", {"input_tokens": 500_000}))  # $1.50
        self.hook(self.event(session_id="other", transcript_path=other),
                  "--daily-budget", "3")
        put(self.t, amsg("m1", {"input_tokens": 300_000}))  # $0.90 + 1.50: under $3
        code, out, _ = self.hook(self.event(), "--daily-budget", "3")
        self.assertIsNone(out)
        put(self.t, amsg("m2", {"input_tokens": 300_000}))  # $1.80 + 1.50 = 3.30
        code, out, _ = self.hook(self.event(), "--daily-budget", "3")
        hso = out["hookSpecificOutput"]
        self.assertEqual(hso["permissionDecision"], "deny")
        self.assertIn("daily budget exceeded", hso["permissionDecisionReason"])
        self.assertIs(out["continue"], False)

    def test_the_project_cap_counts_only_its_project(self):
        other = os.path.join(self.dir, "proj-b", "other.jsonl")
        put(other, amsg("o1", {"input_tokens": 1_000_000}))  # $3, another project
        self.hook(self.event(session_id="other", transcript_path=other),
                  "--project-daily-budget", "100")
        put(self.t, amsg("m1", {"input_tokens": 100_000}))  # $0.30
        code, out, _ = self.hook(self.event(), "--project-daily-budget", "1")
        self.assertIsNone(out)
        put(self.t, amsg("m2", {"input_tokens": 300_000}))
        code, out, _ = self.hook(self.event(), "--project-daily-budget", "1")
        self.assertIn("project daily budget", out["hookSpecificOutput"]["permissionDecisionReason"])

    def test_a_broken_ledger_is_not_a_silently_unchecked_cap(self):
        put(self.t, amsg("m1"))
        blocker = os.path.join(self.dir, "afile")
        with open(blocker, "w", encoding="utf-8"):
            pass
        os.environ["SHORTLEASH_HOME"] = blocker
        code, out, err = self.hook(self.event(), "--daily-budget", "5")
        self.assertEqual(code, 1)
        self.assertIn("NOT checked", err)
        code, out, err = self.hook(self.event(), "--daily-budget", "5", "--fail-closed")
        self.assertEqual(code, 2)

    def test_run_refuses_to_start_without_a_usable_ledger(self):
        blocker = os.path.join(self.dir, "afile")
        with open(blocker, "w", encoding="utf-8"):
            pass
        env = dict(os.environ, SHORTLEASH_HOME=blocker)
        r = subprocess.run([PY, "-m", "shortleash", "run", "--daily-budget", "5",
                            "--", PY, "-c", "print(1)"], cwd=REPO, env=env,
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 2)
        self.assertIn("spend ledger", r.stderr)

    def test_run_stops_when_the_machine_is_over_its_daily_budget(self):
        ledger.record("someone-else", 5.0, "x")
        rec = os.path.join(self.dir, "r.json")
        env = dict(os.environ, SHORTLEASH_HOME=self.home)
        r = subprocess.run([PY, "-m", "shortleash", "run", "--quiet", "--daily-budget", "5.5",
                            "--receipt", rec, "--", PY,
                            os.path.join(REPO, "sim", "runaway.py"), "--delay", "0.2"],
                           cwd=REPO, env=env, capture_output=True, text=True)
        self.assertEqual(r.returncode, 124, r.stderr)
        with open(rec, encoding="utf-8") as f:
            data = json.load(f)
        self.assertIn("daily budget exceeded", data["breach_reason"])
        self.assertGreater(data["ledger"]["day_spend_usd"], 5.5)
        self.assertEqual(data["enforced"], ["daily_budget"])

    def test_without_events_the_daily_cap_is_not_enforced(self):
        env = dict(os.environ, SHORTLEASH_HOME=self.home)
        r = subprocess.run([PY, "-m", "shortleash", "run", "--quiet", "--daily-budget", "5",
                            "--", PY, "-c", "print('hello')"], cwd=REPO, env=env,
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 125)

    def test_project_keys_agree_between_run_and_hook(self):
        self.assertEqual(ledger.project_of_dir("/home/u/my.proj_x"),
                         "-home-u-my-proj-x")
        self.assertEqual(ledger.project_of("/c/projects/-home-u-my-proj-x/s.jsonl"),
                         "-home-u-my-proj-x")


# -- prompts, errors, fuzzy loops ---------------------------------------


class TestPromptBlockAndErrors(HookHarness):
    def test_user_prompt_submit_is_ignored_unless_asked_for(self):
        put(self.t, amsg("m1", {"input_tokens": 2_000_000}))
        ev = self.event(hook_event_name="UserPromptSubmit", prompt="hello")
        code, out, _ = self.hook(ev, "--budget", "1")
        self.assertEqual((code, out), (0, None))

    def test_a_session_over_budget_cannot_take_another_prompt(self):
        put(self.t, amsg("m1", {"input_tokens": 2_000_000}))  # $6
        ev = self.event(hook_event_name="UserPromptSubmit", prompt="one more")
        ev.pop("tool_name"), ev.pop("tool_use_id"), ev.pop("tool_input")
        code, out, _ = self.hook(ev, "--budget", "1", "--block-prompts")
        self.assertEqual(code, 0)
        self.assertEqual(out["decision"], "block")
        self.assertIn("budget exceeded", out["reason"])
        self.assertNotIn("one more", json.dumps(out))
        with open(os.path.join(self.home, hook_mod.HOOK_LOG), encoding="utf-8") as f:
            log = f.read()
        self.assertNotIn("one more", log)

    def test_under_budget_a_prompt_passes_and_ask_over_is_not_a_block(self):
        put(self.t, amsg("m1", {"input_tokens": 100_000}))
        ev = self.event(hook_event_name="UserPromptSubmit")
        code, out, _ = self.hook(ev, "--budget", "5", "--ask-over", "0.1",
                                 "--block-prompts")
        self.assertEqual((code, out), (0, None))

    def test_install_registers_both_events_and_reinstall_replaces(self):
        cmd = "shortleash hook --budget 5 --block-prompts"
        s = hook_mod.merge_settings({"hooks": {"UserPromptSubmit": [
            {"hooks": [{"type": "command", "command": "other-tool"}]}]}},
            cmd, prompts=True)
        ups = s["hooks"]["UserPromptSubmit"]
        self.assertEqual(len(ups), 2)
        self.assertNotIn("matcher", ups[1])
        again = hook_mod.merge_settings(s, cmd, prompts=True)
        self.assertEqual(len(again["hooks"]["UserPromptSubmit"]), 2)
        self.assertEqual(len(again["hooks"]["PreToolUse"]), 1)
        off = hook_mod.merge_settings(s, "shortleash hook --budget 5")
        self.assertEqual(len(off["hooks"]["UserPromptSubmit"]), 1)  # ours removed
        plain = hook_mod.merge_settings({}, "shortleash hook --budget 5")
        self.assertNotIn("UserPromptSubmit", plain["hooks"])

    def test_install_command_carries_the_new_flags(self):
        args = cli_mod._build_parser().parse_args([
            "hook", "install", "--daily-budget", "20", "--project-daily-budget",
            "8", "--max-errors", "6", "--loop-fuzzy", "--block-prompts"])
        cmd = hook_mod.hook_command(args, executable="shortleash")
        for part in ("--daily-budget 20", "--project-daily-budget 8",
                     "--max-errors 6", "--loop-fuzzy", "--block-prompts"):
            self.assertIn(part, cmd)

    def test_bad_values_are_usage_errors(self):
        for argv in (["--max-errors", "1"], ["--daily-budget", "nan"],
                     ["--project-daily-budget", "-1"]):
            with self.assertRaises(SystemExit) as cm, redirect_stderr(io.StringIO()):
                cli_mod.main(["hook", "--budget", "1", *argv])
            self.assertEqual(cm.exception.code, 2, argv)

    def test_error_streak_denies_varied_failing_calls(self):
        for i in range(6):
            put(self.t, amsg(f"m{i}", tools=[(f"t{i}", "Bash", {"command": f"pip install p{i}"})]),
                umsg((f"t{i}", "no network", True)))
        code, out, _ = self.hook(self.event(), "--max-errors", "6")
        self.assertIn("6 tool calls in a row", out["hookSpecificOutput"]["permissionDecisionReason"])
        code, out, _ = self.hook(self.event(), "--max-errors", "7")
        self.assertIsNone(out)

    def test_a_success_resets_the_streak(self):
        for i in range(5):
            put(self.t, amsg(f"m{i}", tools=[(f"t{i}", "Bash", {"command": f"c{i}"})]),
                umsg((f"t{i}", "bad", True)))
        put(self.t, amsg("ok", tools=[("tok", "Bash", {"command": "ok"})]),
            umsg(("tok", "fine", False)))
        put(self.t, amsg("m9", tools=[("t9", "Bash", {"command": "c9"})]),
            umsg(("t9", "bad", True)))
        code, out, _ = self.hook(self.event(), "--max-errors", "3")
        self.assertIsNone(out)

    def test_a_subagent_streak_is_its_own(self):
        for i in range(4):
            put(self.sub, amsg(f"s{i}", tools=[(f"st{i}", "Bash", {"command": f"c{i}"})]),
                umsg((f"st{i}", "bad", True)))
        put(self.t, amsg("m1"))
        code, out, _ = self.hook(self.event(agent_id="1"), "--max-errors", "4")
        self.assertIn("4 tool calls", out["hookSpecificOutput"]["permissionDecisionReason"])
        code, out, _ = self.hook(self.event(), "--max-errors", "4")  # the main agent
        self.assertIsNone(out)

    def test_fuzzy_loop_catches_a_timestamped_retry_exact_does_not(self):
        for i in range(4):
            put(self.t, amsg(f"m{i}", tools=[(f"t{i}", "Bash",
                {"command": f"curl localhost:8080/health?ts={1730000000 + i}"})]),
                umsg((f"t{i}", f"down at 2026-11-07T10:00:0{i}Z", False)))
        ev = self.event(tool_input={"command": "curl localhost:8080/health?ts=1730000009"})
        code, out, _ = self.hook(ev, "--max-repeats", "5")
        self.assertIsNone(out)
        code, out, _ = self.hook(ev, "--max-repeats", "5", "--loop-fuzzy")
        self.assertIn("loop detected", out["hookSpecificOutput"]["permissionDecisionReason"])


class TestFuzzyLoops(unittest.TestCase):
    def test_fingerprint_modes(self):
        a = loops.fingerprint("Bash", {"command": "sleep 5 &&  curl  x?ts=17"}, True)
        b = loops.fingerprint("Bash", {"command": "sleep 9 && curl x?ts=99"}, True)
        self.assertEqual(a, b)
        self.assertNotEqual(loops.fingerprint("Bash", {"command": "sleep 5"}),
                            loops.fingerprint("Bash", {"command": "sleep 9"}))
        self.assertNotEqual(a, loops.fingerprint("Bash", {"command": "curl y"}, True))
        self.assertTrue(loops.fingerprint(None, {"a": object()}, True))

    def test_results_keep_counts_but_drop_timestamps(self):
        f = lambda s: loops.result_digest(s, False, True)  # noqa: E731
        self.assertEqual(f("done in 3.2s at 2026-11-07T10:00:01Z"),
                         f("done in 9.9s at 2026-11-07T11:30:59Z"))
        self.assertEqual(f("pid 4821 id 3f2a9c1e-7b4d-4e0a-9d51-aaaaaaaaaaaa"),
                         f("pid 99 id 00000000-0000-0000-0000-bbbbbbbbbbbb"))
        self.assertNotEqual(f("5 failed"), f("4 failed"))
        self.assertNotEqual(loops.result_digest("x", True, True),
                            loops.result_digest("x", False, True))

    def test_the_masking_is_linear_on_hostile_input(self):
        t0 = time.monotonic()
        loops.fingerprint("Bash", {"command": "9" * 200_000 + " " * 200_000 + "a" * 200_000}, True)
        loops.result_digest("0123456789abcdef" * 20_000 + "1" * 100_000, False, True)
        self.assertLess(time.monotonic() - t0, 5.0)

    def test_the_stream_parser_reports_errors_and_honours_fuzzy(self):
        line = json.dumps(umsg(("t1", "boom", True), ("t2", "fine", False)))
        obs = events.parse_line(line)
        self.assertEqual(obs["errors"], [True, False])
        a = events.parse_line(json.dumps(amsg("m", tools=[("t", "Bash", {"command": "x 1"})])), True)
        b = events.parse_line(json.dumps(amsg("m", tools=[("t", "Bash", {"command": "x 2"})])), True)
        self.assertEqual(a["tool_calls"], b["tool_calls"])

    def test_run_stops_an_error_streak_from_a_stream(self):
        lines = []
        for i in range(5):
            lines.append(json.dumps(amsg(f"m{i}", tools=[(f"t{i}", "Bash", {"command": f"c{i}"})])))
            lines.append(json.dumps(umsg((f"t{i}", "bad", True))))
        code = ("import sys,time\n"
                "for l in sys.argv[1:]: print(l, flush=True)\n"
                "time.sleep(30)")
        rec = os.path.join(tmpdir(), "r.json")
        r = subprocess.run([PY, "-m", "shortleash", "run", "--quiet", "--max-errors", "4",
                            "--receipt", rec, "--history-path", rec + ".h", "--grace-period", "1",
                            "--", PY, "-c", code, *lines],
                           cwd=REPO, capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 124, r.stderr)
        with open(rec, encoding="utf-8") as f:
            self.assertIn("error streak", json.load(f)["breach_reason"])


# -- the receipt chain ---------------------------------------------------


def _chain_writer(args):
    path, i, n = args
    for j in range(n):
        chain.append(path, {"w": i, "j": j})
    return True


class TestChain(unittest.TestCase):
    def setUp(self):
        self.dir = tmpdir()
        self.p = os.path.join(self.dir, "c.jsonl")

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def lines(self):
        with open(self.p, "rb") as f:
            return f.read().split(b"\n")[:-1]

    def fill(self, n=4, key=None):
        for i in range(n):
            head = chain.append(self.p, {"i": i, "cost_usd": 0.1 * i}, key)
        return head

    def test_a_clean_chain_verifies(self):
        head = self.fill()
        res = chain.verify(self.p)
        self.assertTrue(res["ok"])
        self.assertEqual((res["records"], res["head"]), (4, head))

    def test_an_edited_record_is_named(self):
        self.fill()
        ls = self.lines()
        ls[1] = ls[1].replace(b'"cost_usd":0.1', b'"cost_usd":0.0')
        with open(self.p, "wb") as f:
            f.write(b"\n".join(ls) + b"\n")
        res = chain.verify(self.p)
        self.assertFalse(res["ok"])
        self.assertIn("line 2", res["problems"][0])
        self.assertIn("changed", res["problems"][0])

    def test_removed_inserted_and_swapped_records_are_found(self):
        self.fill(5)
        ls = self.lines()
        for new in ([ls[0], ls[2], ls[3], ls[4]],            # removed
                    [ls[0], ls[1], ls[1], ls[2], ls[3]],     # duplicated
                    [ls[0], ls[2], ls[1], ls[3], ls[4]]):    # swapped
            with open(self.p, "wb") as f:
                f.write(b"\n".join(new) + b"\n")
            self.assertFalse(chain.verify(self.p)["ok"])

    def test_cutting_off_the_end_is_seen_only_with_the_head(self):
        head = self.fill(5)
        ls = self.lines()
        with open(self.p, "wb") as f:
            f.write(b"\n".join(ls[:3]) + b"\n")
        self.assertTrue(chain.verify(self.p)["ok"])  # honest: shorter, valid
        res = chain.verify(self.p, expect_head=head)
        self.assertFalse(res["ok"])
        self.assertIn("expected head", res["problems"][0])

    def test_a_rewritten_whole_chain_needs_the_key(self):
        self.fill(3, key=b"s3cret")
        self.assertTrue(chain.verify(self.p, b"s3cret")["ok"])
        self.assertFalse(chain.verify(self.p, b"wrong")["ok"])
        os.remove(self.p)
        self.fill(3, key=None)  # consistent forgery without the key
        self.assertTrue(chain.verify(self.p)["ok"])
        res = chain.verify(self.p, b"s3cret")
        self.assertFalse(res["ok"])
        self.assertIn("MAC", res["problems"][0])

    def test_garbage_and_missing_files(self):
        with open(self.p, "wb") as f:
            f.write(b'{"seq": "x"}\n\xff\xfe\n')
        self.assertFalse(chain.verify(self.p)["ok"])
        self.assertFalse(chain.verify(os.path.join(self.dir, "none"))["ok"])
        with open(self.p, "wb") as f:
            f.write(b"[" * 100000 + b"\n")
        self.assertFalse(chain.verify(self.p)["ok"])

    def test_an_endless_line_is_refused_not_held(self):
        with open(self.p, "wb") as f:
            f.write(b"x" * (chain.MAX_RECORD + 10) + b"\n")
        res = chain.verify(self.p)
        self.assertFalse(res["ok"])
        self.assertIn("too large", res["problems"][0])

    def test_an_incomplete_last_record_is_not_extended(self):
        self.fill(2)
        with open(self.p, "ab") as f:
            f.write(b'{"seq":2,"pr')
        self.assertFalse(chain.verify(self.p)["ok"])
        with self.assertRaises(OSError):
            chain.append(self.p, {"x": 1})

    def test_nan_in_a_receipt_is_refused_not_written(self):
        with self.assertRaises(ValueError):
            chain.append(self.p, {"x": float("nan")})
        self.assertFalse(os.path.exists(self.p) and os.path.getsize(self.p))

    def test_concurrent_runs_extend_one_chain(self):
        ctx = multiprocessing.get_context("spawn" if os.name == "nt" else "fork")
        with ctx.Pool(6) as pool:
            pool.map(_chain_writer, [(self.p, i, 15) for i in range(6)])
        res = chain.verify(self.p)
        self.assertTrue(res["ok"], res["problems"])
        self.assertEqual(res["records"], 90)

    def test_cli_chain_and_verify(self):
        env = dict(os.environ, SHORTLEASH_HOME=os.path.join(self.dir, "h"),
                   SL_KEY="k1")
        for _ in range(2):
            r = subprocess.run([PY, "-m", "shortleash", "run", "--quiet", "--chain", self.p,
                                "--chain-key-env", "SL_KEY", "--history-path",
                                os.path.join(self.dir, "hist"), "--", PY, "-c", "print(1)"],
                               cwd=REPO, env=env, capture_output=True, text=True)
            self.assertIn("receipt chained; head", r.stderr)
        v = subprocess.run([PY, "-m", "shortleash", "verify", self.p, "--key-env", "SL_KEY"],
                           cwd=REPO, env=env, capture_output=True, text=True)
        self.assertEqual(v.returncode, 0, v.stderr)
        self.assertIn("2 record(s)", v.stdout)
        self.assertIn("2 MAC(s) checked", v.stdout)
        bad = subprocess.run([PY, "-m", "shortleash", "verify", self.p, "--key-env", "SL_KEY"],
                             cwd=REPO, env=dict(env, SL_KEY="other"), capture_output=True, text=True)
        self.assertEqual(bad.returncode, 1)
        missing = subprocess.run([PY, "-m", "shortleash", "verify", self.p, "--key-env", "NOPE"],
                                 cwd=REPO, env=env, capture_output=True, text=True)
        self.assertEqual(missing.returncode, 2)

    def test_run_refuses_a_chain_it_cannot_extend(self):
        with open(self.p, "wb") as f:
            f.write(b"not a chain\n")
        r = subprocess.run([PY, "-m", "shortleash", "run", "--quiet", "--chain", self.p,
                            "--", PY, "-c", "print(1)"], cwd=REPO, capture_output=True, text=True,
                           env=dict(os.environ, SHORTLEASH_HOME=os.path.join(self.dir, "h")))
        self.assertEqual(r.returncode, 2)
        self.assertIn("receipt chain", r.stderr)

    def test_the_chained_receipt_has_no_unscrubbed_secret(self):
        fake = "sk-" + "ant-api03-" + "A" * 30  # built at run time
        env = dict(os.environ, SHORTLEASH_HOME=os.path.join(self.dir, "h"))
        subprocess.run([PY, "-m", "shortleash", "run", "--quiet", "--chain", self.p,
                        "--history-path", os.path.join(self.dir, "hist"),
                        "--", PY, "-c", "print(1)", fake], cwd=REPO, env=env,
                       capture_output=True, text=True)
        with open(self.p, encoding="utf-8") as f:
            self.assertNotIn(fake, f.read())


# -- the sentinel ---------------------------------------------------------


@unittest.skipUnless(sys.platform.startswith("linux"), "needs /proc")
class TestSentinel(unittest.TestCase):
    def count(self, tag):
        out = subprocess.run(["pgrep", "-f", tag], capture_output=True, text=True).stdout.split()
        return [int(p) for p in out if int(p) != os.getpid()]

    def settle(self, tag, want=0, timeout=8.0):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            left = self.count(tag)
            if len(left) <= want:
                return left
            time.sleep(0.1)
        return self.count(tag)

    def leash(self, tag, env_extra=None, *flags):
        env = dict(os.environ, SHORTLEASH_HOME=tmpdir(), **(env_extra or {}))
        return subprocess.Popen(
            [PY, "-m", "shortleash", "run", "--quiet", *flags, "--",
             PY, ORPHANS, tag, "--spend"], cwd=REPO, env=env,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            preexec_fn=_isolate.DEFAULT_SIGNALS)

    def cleanup(self, tag):
        for pid in self.count(tag):
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass

    def sleepers_ready(self, tag, n=3, timeout=10.0):
        """All n sleepers are running, and the sentinel (which looks once a
        second) has had time to see them."""
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            out = subprocess.run(["pgrep", "-f", r"sleep\(600\).*" + tag],
                                 capture_output=True, text=True).stdout.split()
            if len(out) >= n:
                time.sleep(1.4)
                return True
            time.sleep(0.1)
        return False

    def test_kill_9_of_the_leash_takes_the_whole_tree(self):
        tag = "sltag" + os.urandom(6).hex()
        p = self.leash(tag, None, "--budget", "1000")
        try:
            self.assertTrue(self.sleepers_ready(tag), self.count(tag))
            p.send_signal(signal.SIGKILL)
            p.wait(10)
            self.assertEqual(self.settle(tag), [])
        finally:
            self.cleanup(tag)

    def test_without_the_sentinel_the_orphans_survive(self):
        tag = "sltag" + os.urandom(6).hex()
        p = self.leash(tag, {proctree.NO_ESCAPE_ENV: "1"}, "--budget", "1000")
        try:
            self.assertTrue(self.sleepers_ready(tag), self.count(tag))
            p.send_signal(signal.SIGKILL)
            p.wait(10)
            time.sleep(1.5)
            self.assertGreaterEqual(len(self.count(tag)), 2)
        finally:
            self.cleanup(tag)

    def test_a_budget_breach_also_kills_the_detached_and_the_daemon(self):
        tag = "sltag" + os.urandom(6).hex()
        p = self.leash(tag, None, "--budget", "0.5")
        try:
            p.wait(60)
            self.assertEqual(self.settle(tag), [])
        finally:
            self.cleanup(tag)

    def test_a_normal_exit_does_not_kill_anything_the_sentinel_should_spare(self):
        # the sentinel stands down on a clean finish: no stray process left
        env = dict(os.environ, SHORTLEASH_HOME=tmpdir())
        r = subprocess.run([PY, "-m", "shortleash", "run", "--quiet", "--",
                            PY, "-c", "print('hi')"], cwd=REPO, env=env,
                           capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, 0)
        time.sleep(0.3)
        out = subprocess.run(["pgrep", "-f", "shortleash.*-I -c"], capture_output=True, text=True)
        self.assertEqual(out.stdout.split(), [])

    def test_process_table_and_descendants(self):
        child = subprocess.Popen([PY, "-c", "import time; time.sleep(5)"])
        try:
            table = proctree.process_table()
            self.assertIn(child.pid, table)
            self.assertEqual(table[child.pid][0], os.getpid())
            self.assertIn(child.pid, proctree.descendants(os.getpid(), table))
        finally:
            child.kill()
            child.wait()


if __name__ == "__main__":
    unittest.main()
