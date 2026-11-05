"""0.2.0: loop and stuck detection, the approval gate, the Claude Code hook,
per-message model pricing, spend forecasts and the Markdown receipt."""

import _isolate  # noqa: F401  (isolate ~/.shortleash before importing)

import io
import json
import os
import shutil
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

from shortleash import cli as cli_mod
from shortleash import events, loops, paths, proctree
from shortleash import hook as hook_mod
from shortleash import receipt as receipt_mod
from shortleash.guard import Guard, BREACH_EXIT_CODE

PY = sys.executable


def _tmp() -> str:
    d = tempfile.mkdtemp(prefix="sl-s1-")
    return d


def lingerer(*lines: str, sleep: float = 30.0) -> list:
    """A child that prints the lines, then sleeps (so only the leash can
    end it early)."""
    code = ("import sys, time\n"
            "for l in sys.argv[2:]: print(l, flush=True)\n"
            "time.sleep(float(sys.argv[1]))")
    return [PY, "-c", code, str(sleep), *lines]


INIT = json.dumps({"type": "system", "subtype": "init",
                   "model": "claude-sonnet-4-5", "session_id": "s"})


def assistant(mid, *tools, usage=None, model=None):
    content = [{"type": "tool_use", "id": tid, "name": name, "input": inp}
               for tid, name, inp in tools]
    msg = {"id": mid, "content": content,
           "usage": usage or {"input_tokens": 10, "output_tokens": 5}}
    if model:
        msg["model"] = model
    return json.dumps({"type": "assistant", "message": msg})


def run_guard(g: Guard, argv: list) -> tuple:
    tmp = _tmp()
    g.receipt_path = os.path.join(tmp, "r.json")
    g.history_path = os.path.join(tmp, "h.jsonl")
    err = io.StringIO()
    t0 = time.monotonic()
    with redirect_stderr(err), redirect_stdout(io.StringIO()):
        code = g.run(argv)
    took = time.monotonic() - t0
    with open(g.receipt_path, encoding="utf-8") as f:
        rec = json.load(f)
    shutil.rmtree(tmp, ignore_errors=True)
    return code, rec, err.getvalue(), took


def cli(*argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        try:
            code = cli_mod.main(list(argv))
        except SystemExit as e:
            code = e.code
    return code, out.getvalue(), err.getvalue()


# ---------------------------------------------------------------------
# loops

class TestLoops(unittest.TestCase):
    def fp(self, name, **inp):
        return loops.fingerprint(name, inp)

    def test_fingerprint_ignores_key_order_and_hides_input(self):
        a = loops.fingerprint("Bash", {"command": "ls", "timeout": 5})
        b = loops.fingerprint("Bash", {"timeout": 5, "command": "ls"})
        self.assertEqual(a, b)
        self.assertNotEqual(a, loops.fingerprint("Bash", {"command": "ls -a"}))
        secret = loops.fingerprint("Bash", {"command": "curl -H 'token: s3cr3t'"})
        self.assertNotIn("s3cr3t", secret)
        self.assertNotIn("s3cr3t", loops.describe([secret], 3))

    def test_fingerprint_never_raises(self):
        deep = cur = []
        for _ in range(100000):
            cur.append([])
            cur = cur[0]
        self.assertTrue(loops.fingerprint(None, deep).startswith("?#"))
        self.assertTrue(loops.fingerprint("x" * 10000, {1: object()}))

    def test_period_one_two_three(self):
        A, B, C = self.fp("A"), self.fp("B"), self.fp("C")
        self.assertEqual(loops.find_loop([B, A, A, A], 3), [A])
        self.assertIsNone(loops.find_loop([A, A], 3))
        self.assertEqual(loops.find_loop([A, B, A, B, A, B], 3), [A, B])
        self.assertEqual(loops.find_loop([C, A, B, C, A, B, C], 2), [A, B, C])
        self.assertIsNone(loops.find_loop([A, A, A], 1))
        self.assertIsNone(loops.find_loop([A, A, A], None))

    def test_an_edit_test_cycle_is_not_a_loop(self):
        seq = []
        for i in range(50):
            seq.append(self.fp("Edit", file="a.py", new=f"attempt {i}"))
            seq.append(self.fp("Bash", command="pytest"))
        self.assertIsNone(loops.find_loop(seq, 3))

    def test_events_carry_tool_calls_and_model(self):
        obs = events.parse_line(assistant(
            "m1", ("t1", "Bash", {"command": "ls"}), model="claude-opus-4-5"))
        self.assertEqual(obs["model"], "claude-opus-4-5")
        self.assertEqual([c for c, _ in obs["tool_calls"]], ["t1"])
        self.assertTrue(obs["tool_calls"][0][1].startswith("Bash#"))


# ---------------------------------------------------------------------
# the leash: loop, stuck, forecast, per-model pricing, markdown

class TestRunCaps(unittest.TestCase):
    def test_a_repeated_call_trips_the_loop_cap_live(self):
        lines = [INIT] + [assistant(f"m{i}", (f"t{i}", "Bash", {"command": "npm test"}))
                          for i in range(6)]
        g = Guard(max_repeats=4, quiet=True, grace_period=0.5)
        code, rec, _, took = run_guard(g, lingerer(*lines))
        self.assertEqual(code, BREACH_EXIT_CODE)
        self.assertTrue(rec["breached"])
        self.assertIn("loop detected: the same Bash call", rec["breach_reason"])
        self.assertIn("4 times in a row", rec["breach_reason"])
        self.assertNotIn("npm test", rec["breach_reason"])
        self.assertIn("max_repeats", rec["enforced"])
        self.assertTrue(rec["enforced_live"]["max_repeats"])
        self.assertLess(took, 20)

    def test_a_re_emitted_block_is_one_call_not_a_loop(self):
        same = assistant("m1", ("t1", "Bash", {"command": "ls"}))
        g = Guard(max_repeats=2, quiet=True)
        code, rec, _, _ = run_guard(g, lingerer(INIT, same, same, same, sleep=0))
        self.assertEqual(code, 0)
        self.assertFalse(rec["breached"])

    def test_loop_cap_without_events_is_not_enforced(self):
        g = Guard(max_repeats=3, quiet=True)
        code, rec, _, _ = run_guard(g, lingerer("plain text", sleep=0))
        self.assertEqual(code, 125)
        self.assertIn("max_repeats", rec["not_enforced"])

    def test_idle_timeout_stops_a_silent_agent(self):
        g = Guard(idle_timeout=1.0, quiet=True, grace_period=0.5)
        code, rec, _, took = run_guard(g, lingerer(INIT, sleep=60))
        self.assertEqual(code, BREACH_EXIT_CODE)
        self.assertIn("stuck: no output from the agent", rec["breach_reason"])
        self.assertIn("idle_timeout", rec["enforced"])
        self.assertTrue(rec["enforced_live"]["idle_timeout"])
        self.assertLess(took, 15)

    def test_idle_timeout_leaves_a_chatty_agent_alone(self):
        code = ("import time\n"
                "for i in range(8):\n"
                "    print('tick', flush=True); time.sleep(0.25)")
        g = Guard(idle_timeout=1.5, quiet=True)
        rc, rec, _, _ = run_guard(g, [PY, "-c", code])
        self.assertEqual(rc, 0)
        self.assertFalse(rec["breached"])

    def test_forecast_lines_at_half_and_four_fifths(self):
        big = {"input_tokens": 100_000, "output_tokens": 0}  # $0.30 each
        lines = [INIT] + [assistant(f"m{i}", usage=big) for i in range(3)]
        g = Guard(budget=1.0)
        g.quiet = False
        code, rec, err, _ = run_guard(g, lingerer(*lines, sleep=0))
        self.assertEqual(code, 0)
        self.assertIn("50% of budget used", err)
        self.assertIn("80% of budget used", err)
        self.assertIn("at this rate the cap is reached in ~", err)
        self.assertEqual(rec["budget_used_pct"] is None, rec["cost_usd"] is None)

    def test_forecast_off_and_quiet(self):
        big = {"input_tokens": 300_000, "output_tokens": 0}
        g = Guard(budget=1.0, warn_at=())
        code, rec, err, _ = run_guard(g, lingerer(INIT, assistant("m", usage=big),
                                                  sleep=0))
        self.assertNotIn("of budget used", err)

    def test_each_message_priced_at_its_own_model(self):
        u = {"input_tokens": 1_000_000}
        lines = [INIT, assistant("m1", usage=u),
                 assistant("m2", usage=u, model="claude-opus-4-1")]
        g = Guard(budget=1000.0, quiet=True)
        _, rec, _, _ = run_guard(g, lingerer(*lines, sleep=0))
        self.assertAlmostEqual(rec["cost_usd"], 18.0)  # 3 (sonnet) + 15 (opus)
        self.assertEqual(set(rec["models"]), {"claude-sonnet-4-5",
                                              "claude-opus-4-1"})

    def test_budget_trips_on_the_dearer_subagent_model(self):
        u = {"input_tokens": 100_000}
        lines = [INIT] + [assistant(f"m{i}", usage=u, model="claude-opus-4-1")
                          for i in range(3)]
        # sonnet would be $0.30/msg -> under $2; opus is $1.50/msg -> over
        g = Guard(budget=2.0, quiet=True, grace_period=0.5)
        code, rec, _, _ = run_guard(g, lingerer(*lines))
        self.assertEqual(code, BREACH_EXIT_CODE)
        self.assertIn("budget exceeded", rec["breach_reason"])

    def test_a_flood_of_model_names_stays_bounded(self):
        lines = [INIT] + [assistant(f"m{i}", model=f"x{i}" * 100)
                          for i in range(200)]
        g = Guard(budget=1000.0, quiet=True)
        _, rec, _, _ = run_guard(g, lingerer(*lines, sleep=0))
        self.assertLessEqual(len(rec["models"]), 33)
        self.assertTrue(all(len(m) <= 200 for m in rec["models"]))

    def test_markdown_receipt_is_appended_and_escaped(self):
        tmp = _tmp()
        md = os.path.join(tmp, "sub", "summary.md")
        bad = json.dumps({"type": "assistant", "message": {
            "id": "x", "content": [], "usage": {"input_tokens": "<b>|[x](y)"}}})
        for _ in range(2):
            g = Guard(budget=5.0, quiet=True, receipt_md=md)
            run_guard(g, lingerer(INIT, bad, sleep=0))
        with open(md, encoding="utf-8") as f:
            text = f.read()
        self.assertEqual(text.count("### shortleash receipt"), 2)
        self.assertIn("**STOPPED**: malformed telemetry", text)
        self.assertNotIn("<b>", text)
        self.assertIn("&lt;b&gt;\\|\\[x\\](y)", text)
        self.assertNotIn(PY, text)  # the command is never in the summary
        shutil.rmtree(tmp, ignore_errors=True)

    def test_unwritable_markdown_path_is_refused_before_the_run(self):
        tmp = _tmp()
        blocker = os.path.join(tmp, "file")
        open(blocker, "w").close()
        g = Guard(quiet=True, receipt_md=os.path.join(blocker, "x.md"))
        with redirect_stderr(io.StringIO()) as err:
            code = g.run(lingerer("hi", sleep=0))
        self.assertEqual(code, 2)
        self.assertIn("markdown receipt", err.getvalue())
        shutil.rmtree(tmp, ignore_errors=True)


# ---------------------------------------------------------------------
# approval gate (run)

def _state(pid):
    try:
        with open(f"/proc/{pid}/stat") as f:
            return f.read().rsplit(")", 1)[1].split()[0]
    except OSError:
        return None


class TestApprovalGate(unittest.TestCase):
    LINES = [INIT] + [assistant(f"m{i}", usage={"input_tokens": 100_000})
                      for i in range(4)]  # $0.30 each at sonnet rates

    def test_yes_lets_it_continue_and_it_was_paused_meanwhile(self):
        seen = {}

        def ask(text, timeout, cancelled):
            seen["text"] = text
            seen["state"] = _state(g.proc.pid)
            return "yes"
        g = Guard(budget=10.0, ask_over=0.5, ask_fn=ask, quiet=True)
        code, rec, _, _ = run_guard(g, lingerer(*self.LINES, sleep=0))
        if not proctree.can_pause():
            self.skipTest("no SIGSTOP here")
        self.assertEqual(code, 0)
        self.assertFalse(rec["breached"])
        self.assertEqual(rec["approval"]["answer"], "yes")
        self.assertIn("APPROVAL NEEDED", seen["text"])
        self.assertIn("$10.00 budget", seen["text"])
        if sys.platform.startswith("linux"):
            self.assertIn(seen["state"], ("T", "t"))  # stopped while asking

    def test_no_stops_the_run(self):
        g = Guard(ask_over=0.5, ask_fn=lambda *a: "no", quiet=True,
                  grace_period=0.5)
        code, rec, _, _ = run_guard(g, lingerer(*self.LINES))
        self.assertEqual(code, BREACH_EXIT_CODE)
        self.assertIn("approval refused", rec["breach_reason"])
        self.assertIn("ask_over", rec["enforced"])

    def test_no_terminal_fails_closed(self):
        g = Guard(ask_over=0.5, ask_fn=lambda *a: None, quiet=True,
                  grace_period=0.5)
        code, rec, _, _ = run_guard(g, lingerer(*self.LINES))
        self.assertEqual(code, BREACH_EXIT_CODE)
        self.assertIn("no terminal", rec["breach_reason"])

    def test_a_platform_that_cannot_pause_stops_at_the_threshold(self):
        asked = []
        with mock.patch.object(proctree, "can_pause", return_value=False):
            g = Guard(ask_over=0.5, ask_fn=lambda *a: asked.append(1) or "yes",
                      quiet=True, grace_period=0.5)
            code, rec, _, _ = run_guard(g, lingerer(*self.LINES))
        self.assertEqual(asked, [])
        self.assertEqual(code, BREACH_EXIT_CODE)
        self.assertIn("cannot pause", rec["breach_reason"])
        self.assertEqual(rec["approval"]["answer"], "unavailable")

    def test_the_timeout_still_kills_a_paused_agent(self):
        def ask(text, timeout, cancelled):
            while not cancelled():
                time.sleep(0.05)
            return "no"
        g = Guard(ask_over=0.5, timeout=2.0, ask_fn=ask, quiet=True,
                  grace_period=1.0)
        code, rec, _, took = run_guard(g, lingerer(*self.LINES, sleep=60))
        self.assertEqual(code, BREACH_EXIT_CODE)
        self.assertIn("time limit exceeded", rec["breach_reason"])
        self.assertLess(took, 20)

    def test_cli_refuses_a_gate_above_the_budget(self):
        code, _, err = cli("run", "--budget", "1", "--ask-over", "1", "--",
                           PY, "-c", "pass")
        self.assertEqual(code, 2)
        self.assertIn("must be below", err)

    def test_cli_validation_of_the_new_options(self):
        for argv in (["--max-repeats", "1"], ["--idle-timeout", "0"],
                     ["--idle-timeout", "nan"], ["--warn-at", "1.5"],
                     ["--warn-at", "x"], ["--ask-timeout", "0"]):
            code, _, err = cli("run", *argv, "--", PY, "-c", "pass")
            self.assertEqual(code, 2, argv)


# ---------------------------------------------------------------------
# the Claude Code hook

def _line(obj):
    return json.dumps(obj) + "\n"


class TestHook(unittest.TestCase):
    def setUp(self):
        self.tmp = _tmp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.t = os.path.join(self.tmp, "sess.jsonl")
        os.makedirs(os.path.join(self.tmp, "sess", "subagents"))
        self.sub = os.path.join(self.tmp, "sess", "subagents", "agent-a1.jsonl")
        self.home = os.path.join(self.tmp, "state")
        patcher = mock.patch.dict(os.environ, {paths.STATE_ENV: self.home})
        patcher.start()
        self.addCleanup(patcher.stop)

    def write(self, path, *entries, mode="w"):
        with open(path, mode, encoding="utf-8") as f:
            for e in entries:
                f.write(e if isinstance(e, str) else _line(e))

    @staticmethod
    def amsg(mid, usage, model="claude-sonnet-4-5", tools=()):
        return {"type": "assistant", "message": {
            "id": mid, "model": model, "usage": usage,
            "content": [{"type": "tool_use", "id": t, "name": n, "input": i}
                        for t, n, i in tools]}}

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

    def test_under_every_cap_it_says_nothing(self):
        self.write(self.t, self.amsg("m1", {"input_tokens": 1000}))
        code, out, _ = self.hook(self.event(), "--budget", "5", "--max-repeats", "3")
        self.assertEqual((code, out), (0, None))

    def test_budget_counts_subagents_dedupes_and_stops(self):
        u = {"input_tokens": 500_000}  # $1.50 at sonnet rates
        # the same message twice (one line per content block): counted once
        self.write(self.t, self.amsg("m1", u), self.amsg("m1", u), "not json\n",
                   '{"type":"assistant","message":{"id":"broken"', "\n")
        code, out, _ = self.hook(self.event(), "--budget", "2")
        self.assertIsNone(out)
        self.write(self.sub, self.amsg("s1", u, model="claude-sonnet-4-5"))
        code, out, _ = self.hook(self.event(), "--budget", "2")
        self.assertEqual(code, 0)
        hso = out["hookSpecificOutput"]
        self.assertEqual(hso["hookEventName"], "PreToolUse")
        self.assertEqual(hso["permissionDecision"], "deny")
        self.assertIn("budget exceeded: $3.0000", hso["permissionDecisionReason"])
        self.assertIs(out["continue"], False)
        self.assertIn("budget exceeded", out["stopReason"])
        with open(os.path.join(self.home, hook_mod.HOOK_LOG), encoding="utf-8") as f:
            log = [json.loads(x) for x in f]
        self.assertEqual(log[-1]["decision"], "deny")
        self.assertNotIn("ls", json.dumps(log[-1]["reason"]))

    def test_the_larger_of_estimate_and_recorded_cost_is_enforced(self):
        self.write(self.t, self.amsg("m1", {"input_tokens": 10}),
                   {"type": "cost-state", "totalCostUSD": 9.5})
        _, out, _ = self.hook(self.event(), "--budget", "5")
        self.assertIn("Claude Code's own recorded total",
                      out["hookSpecificOutput"]["permissionDecisionReason"])
        # a junk figure is ignored, not trusted
        self.write(self.t, self.amsg("m1", {"input_tokens": 10}),
                   {"type": "cost-state", "totalCostUSD": "NaN"})
        self.assertIsNone(self.hook(self.event(), "--budget", "5")[1])

    def test_ask_over_is_the_human_approval_gate(self):
        self.write(self.t, self.amsg("m1", {"input_tokens": 500_000}))  # $1.50
        _, out, _ = self.hook(self.event(), "--budget", "5", "--ask-over", "1")
        hso = out["hookSpecificOutput"]
        self.assertEqual(hso["permissionDecision"], "ask")
        self.assertIn("approval threshold", hso["permissionDecisionReason"])
        self.assertNotIn("continue", out)

    def test_spawn_cap_denies_the_spawn_not_other_tools(self):
        self.write(self.t, self.amsg("m1", {"input_tokens": 1},
                                     tools=[("a1", "Agent", {"p": 1}),
                                            ("a2", "Task", {"p": 2})]))
        _, out, _ = self.hook(self.event(tool_name="Agent", tool_use_id="a3"),
                              "--max-spawns", "2")
        self.assertEqual(out["hookSpecificOutput"]["permissionDecision"], "deny")
        self.assertIn("subagent 3 of at most 2",
                      out["hookSpecificOutput"]["permissionDecisionReason"])
        self.assertNotIn("continue", out)
        # the call already in the transcript is not counted twice
        self.assertIsNone(self.hook(self.event(tool_name="Agent", tool_use_id="a2"),
                                    "--max-spawns", "2")[1])
        self.assertIsNone(self.hook(self.event(), "--max-spawns", "2")[1])

    def test_loop_is_denied_with_a_reason(self):
        calls = [self.amsg(f"m{i}", {"input_tokens": 1},
                           tools=[(f"t{i}", "Bash", {"command": "make"})])
                 for i in range(2)]
        self.write(self.t, *calls)
        _, out, _ = self.hook(self.event(tool_input={"command": "make"},
                                         tool_use_id="t9"), "--max-repeats", "3")
        reason = out["hookSpecificOutput"]["permissionDecisionReason"]
        self.assertIn("loop detected", reason)
        self.assertNotIn("make", reason)
        # a different call is fine
        self.assertIsNone(self.hook(self.event(tool_input={"command": "ls"}),
                                    "--max-repeats", "3")[1])
        # the call already written to the transcript is judged where it is
        self.assertIsNone(self.hook(self.event(tool_input={"command": "make"},
                                               tool_use_id="t1"),
                                    "--max-repeats", "3")[1])

    def test_loop_inside_a_subagent_uses_its_own_transcript(self):
        self.write(self.t, self.amsg("m1", {"input_tokens": 1}))
        self.write(self.sub, *[self.amsg(f"s{i}", {"input_tokens": 1},
                                         tools=[(f"t{i}", "Read", {"f": "a"})])
                               for i in range(3)])
        _, out, _ = self.hook(self.event(agent_id="a1", tool_name="Read",
                                         tool_input={"f": "a"}),
                              "--max-repeats", "4")
        self.assertIn("loop detected", out["hookSpecificOutput"]["permissionDecisionReason"])
        self.assertIsNone(self.hook(self.event(tool_name="Read",
                                               tool_input={"f": "a"}),
                                    "--max-repeats", "4")[1])

    def test_bad_input_fails_open_unless_fail_closed(self):
        code, out, err = self.hook("not json", "--budget", "1")
        self.assertEqual((code, out), (1, None))
        self.assertIn("not JSON", err)
        code, _, _ = self.hook("[]", "--budget", "1", "--fail-closed")
        self.assertEqual(code, 2)
        code, _, err = self.hook(self.event(transcript_path=None), "--budget", "1")
        self.assertEqual(code, 1)
        self.assertEqual(self.hook("x" * (hook_mod.MAX_INPUT + 10), "--budget", "1")[0], 1)

    def test_missing_transcript_is_zero_spend_and_other_events_are_ignored(self):
        os.remove(self.t) if os.path.exists(self.t) else None
        self.assertEqual(self.hook(self.event(), "--budget", "0.01")[:2], (0, None))
        self.write(self.t, self.amsg("m1", {"input_tokens": 10_000_000}))
        self.assertEqual(self.hook(self.event(hook_event_name="PostToolUse"),
                                   "--budget", "0.01")[:2], (0, None))

    def test_odd_field_types_do_not_crash(self):
        self.write(self.t, self.amsg("m1", {"input_tokens": 1}))
        code, out, _ = self.hook(self.event(tool_use_id=["x"], tool_name=7,
                                            agent_id={"a": 1}),
                                 "--max-spawns", "0", "--max-repeats", "2")
        self.assertEqual(code, 0)

    def test_cli_hook_reads_stdin(self):
        self.write(self.t, self.amsg("m1", {"input_tokens": 10_000_000}))
        with mock.patch.object(sys, "stdin", io.StringIO(json.dumps(self.event()))):
            code, out, _ = cli("hook", "--budget", "1")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["hookSpecificOutput"]["permissionDecision"],
                         "deny")


class TestHookInstall(unittest.TestCase):
    def setUp(self):
        self.tmp = _tmp()
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def test_print_mode(self):
        code, out, _ = cli("hook", "install", "--budget", "5", "--ask-over", "3")
        self.assertEqual(code, 0)
        block = json.loads(out[out.index("{"):])
        cmd = block["hooks"]["PreToolUse"][0]["hooks"][0]["command"]
        self.assertIn("hook --budget 5 --ask-over 3", cmd)

    def test_needs_a_cap_and_a_sane_gate(self):
        self.assertEqual(cli("hook", "install")[0], 2)
        self.assertEqual(cli("hook", "install", "--budget", "2",
                             "--ask-over", "3")[0], 2)
        self.assertEqual(cli("hook", "install", "--budget", "-1")[0], 2)

    def test_write_merges_keeps_others_and_replaces_ours(self):
        path = os.path.join(self.tmp, ".claude", "settings.json")
        os.makedirs(os.path.dirname(path))
        other = {"matcher": "Bash", "hooks": [{"type": "command",
                                               "command": "./mine.sh"}]}
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"model": "x", "hooks": {"PreToolUse": [other],
                                               "Stop": [1]}}, f)
        self.assertEqual(cli("hook", "install", "--budget", "5", "--write", path)[0], 0)
        self.assertEqual(cli("hook", "install", "--budget", "7", "--write", path)[0], 0)
        with open(path, encoding="utf-8") as f:
            s = json.load(f)
        self.assertEqual(s["model"], "x")
        self.assertEqual(s["hooks"]["Stop"], [1])
        pre = s["hooks"]["PreToolUse"]
        self.assertEqual(pre[0], other)
        self.assertEqual(len(pre), 2)
        self.assertIn("--budget 7", pre[1]["hooks"][0]["command"])
        self.assertTrue(os.path.exists(path + ".bak"))

    def test_write_refuses_a_broken_settings_file(self):
        path = os.path.join(self.tmp, "settings.json")
        with open(path, "w", encoding="utf-8") as f:
            f.write("{oops")
        code, _, err = cli("hook", "install", "--budget", "5", "--write", path)
        self.assertEqual(code, 2)
        self.assertIn("not valid JSON", err)
        with open(path, encoding="utf-8") as f:
            self.assertEqual(f.read(), "{oops")
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"hooks": []}, f)
        self.assertEqual(cli("hook", "install", "--budget", "5", "--write", path)[0], 2)


class TestReceiptFields(unittest.TestCase):
    def test_burn_rate_and_budget_share(self):
        rec = receipt_mod.build_receipt(
            command=["x"], started_at=0.0, ended_at=120.0, breached=False,
            breach_reason=None, cost_usd=1.0, cost_estimated=True,
            usage={}, num_turns=1, spawns=0, exit_code=0, model="m",
            session_id="s", budget=4.0)
        self.assertAlmostEqual(rec["burn_rate_usd_per_min"], 0.5)
        self.assertEqual(rec["budget_used_pct"], 25.0)
        self.assertIn("rate:      $0.5000/min   budget used: 25%",
                      receipt_mod.format_human(rec))


if __name__ == "__main__":
    unittest.main()
