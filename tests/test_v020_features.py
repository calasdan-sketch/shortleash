"""0.2.0: spawn-storm guard, culprit in the receipt, .shortleash.toml,
early-warning forecast, Claude Code hook mode."""

import _isolate  # noqa: F401  (isolate ~/.shortleash before importing)

import contextlib
import io
import json
import os
import shlex
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

from shortleash import cli as cli_mod
from shortleash import events
from shortleash import forecast
from shortleash import guard as guard_mod
from shortleash import hookmode
from shortleash import profile
from shortleash import receipt as receipt_mod
from shortleash.guard import Guard

PY = sys.executable


def tmpdir():
    d = tempfile.mkdtemp(prefix="sl20-")
    return d


def write(path, text):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="") as f:
        f.write(text)


def assistant(msg_id, *, spawns=(), parent=None, tokens=100):
    """One assistant stream line. spawns = [(tool_use_id, subagent_type)]."""
    content = [{"type": "text", "text": "x"}]
    for sid, kind in spawns:
        block = {"type": "tool_use", "id": sid, "name": "Agent",
                 "input": {"description": "d", "prompt": "p"}}
        if kind is not None:
            block["input"]["subagent_type"] = kind
        content.append(block)
    ev = {"type": "assistant", "parent_tool_use_id": parent,
          "message": {"id": msg_id, "content": content,
                      "usage": {"input_tokens": tokens, "output_tokens": 0,
                                "cache_creation_input_tokens": 0,
                                "cache_read_input_tokens": 0}}}
    return json.dumps(ev)


def feed(g, *lines):
    for line in lines:
        g._process_line(line)


# ---------------------------------------------------------------------------
class TestEventsCarryTheAgentType(unittest.TestCase):
    def test_spawn_types_and_parent(self):
        obs = events.parse_line(assistant(
            "m1", spawns=[("t1", "Explore"), ("t2", None)], parent="p9"))
        self.assertEqual(obs["spawn_types"], {"t1": "Explore", "t2": "agent"})
        self.assertEqual(obs["parent_tool_use_id"], "p9")

    def test_main_conversation_has_no_parent(self):
        obs = events.parse_line(assistant("m1"))
        self.assertIsNone(obs["parent_tool_use_id"])
        self.assertEqual(obs["spawn_types"], {})

    def test_old_task_tool_counts_too(self):
        line = json.loads(assistant("m1", spawns=[("t1", "Plan")]))
        line["message"]["content"][1]["name"] = "Task"
        obs = events.parse_line(json.dumps(line))
        self.assertEqual(obs["spawn_types"], {"t1": "Plan"})

    def test_hostile_names_are_cleaned_and_bounded(self):
        obs = events.parse_line(assistant(
            "m1", spawns=[("t1", "A\x1b[31mB" + "z" * 500), ("t2", 42)]))
        name = obs["spawn_types"]["t1"]
        self.assertLessEqual(len(name), events.MAX_AGENT_NAME)
        self.assertNotIn("\x1b", name)
        self.assertEqual(obs["spawn_types"]["t2"], "agent")


# ---------------------------------------------------------------------------
class TestCulpritNamesTheAgent(unittest.TestCase):
    def storm(self, g, n=38, agent_tokens=7100, main_tokens=2900):
        feed(g, assistant("main0", tokens=main_tokens))
        for i in range(n):
            feed(g, assistant(f"m{i}", spawns=[(f"t{i}", "Explore")],
                              tokens=0))
        feed(g, assistant("sub0", parent="t0", tokens=agent_tokens))

    def test_the_headline_sentence(self):
        g = Guard(quiet=True)
        self.storm(g)
        c = g.culprit()
        self.assertEqual(c["text"], "38 spawns, 71% of tokens in agent Explore")
        self.assertEqual(c["agent"], "Explore")
        self.assertEqual(c["agent_spawns"], 38)

    def test_main_conversation_can_be_the_biggest(self):
        g = Guard(quiet=True)
        self.storm(g, n=3, agent_tokens=100, main_tokens=900)
        c = g.culprit()
        self.assertEqual(c["agent"], "main")
        self.assertIn("main conversation", c["text"])

    def test_none_without_spawns_or_tokens(self):
        g = Guard(quiet=True)
        self.assertIsNone(g.culprit())
        feed(g, assistant("m1", tokens=500))
        self.assertIsNone(g.culprit())

    def test_the_busiest_of_several_agents_is_named(self):
        g = Guard(quiet=True)
        feed(g, assistant("a", spawns=[("t1", "Explore"), ("t2", "Plan")],
                          tokens=0))
        feed(g, assistant("b", parent="t1", tokens=100),
             assistant("c", parent="t2", tokens=900))
        self.assertEqual(g.culprit()["agent"], "Plan")

    def test_an_unknown_parent_is_counted_as_subagent(self):
        g = Guard(quiet=True)
        feed(g, assistant("a", spawns=[("t1", "Explore")], tokens=0),
             assistant("b", parent="never-seen", tokens=500))
        self.assertEqual(g.agent_tokens["subagent"], 500)

    def test_a_repeated_message_is_not_counted_twice(self):
        g = Guard(quiet=True)
        feed(g, assistant("a", spawns=[("t1", "Explore")], tokens=0),
             assistant("b", parent="t1", tokens=500),
             assistant("b", parent="t1", tokens=500))
        self.assertEqual(g.agent_tokens["Explore"], 500)

    def test_a_retried_spawn_block_counts_once(self):
        g = Guard(quiet=True)
        feed(g, assistant("a", spawns=[("t1", "Explore")]),
             assistant("a2", spawns=[("t1", "Explore")]))
        self.assertEqual(g.spawns, 1)
        self.assertEqual(g.agent_spawns["Explore"], 1)

    def test_distinct_names_are_bounded(self):
        g = Guard(quiet=True)
        for i in range(guard_mod._MAX_AGENT_KINDS + 20):
            feed(g, assistant(f"m{i}", spawns=[(f"t{i}", f"kind{i}")]))
        self.assertLessEqual(len(g.agent_spawns), guard_mod._MAX_AGENT_KINDS + 1)
        self.assertIn("(other)", g.agent_spawns)
        self.assertEqual(g.spawns, guard_mod._MAX_AGENT_KINDS + 20)

    def test_breakdown_is_bounded_and_sorted(self):
        g = Guard(quiet=True)
        for i in range(15):
            feed(g, assistant(f"m{i}", spawns=[(f"t{i}", f"k{i}")], tokens=i))
        rows = g.agent_breakdown()
        self.assertLessEqual(len(rows), 10)

    def test_receipt_and_human_text(self):
        g = Guard(quiet=True)
        self.storm(g)
        rec = receipt_mod.build_receipt(
            command=["x"], started_at=0, ended_at=1, breached=True,
            breach_reason="spawn cap exceeded: 38 subagents > 10 max",
            cost_usd=1.0, cost_estimated=True, usage=g.usage, num_turns=1,
            spawns=g.spawns, exit_code=3, model="m", session_id="s",
            culprit=g.culprit(), agents=g.agent_breakdown())
        self.assertEqual(rec["culprit"]["agent"], "Explore")
        self.assertIn("Explore", rec["agents"])
        text = receipt_mod.format_human(rec)
        self.assertIn("culprit:   38 spawns, 71% of tokens in agent Explore",
                      text)
        json.dumps(rec)

    def test_no_culprit_line_without_a_breach(self):
        rec = receipt_mod.build_receipt(
            command=["x"], started_at=0, ended_at=1, breached=False,
            breach_reason=None, cost_usd=1.0, cost_estimated=True,
            usage={}, num_turns=1, spawns=2, exit_code=0, model="m",
            session_id="s", culprit={"text": "2 spawns, 50% of tokens in "
                                            "agent Plan"})
        self.assertNotIn("culprit:", receipt_mod.format_human(rec))


# ---------------------------------------------------------------------------
STORM_AGENT = r'''
import json, sys, time
def emit(o):
    sys.stdout.write(json.dumps(o) + "\n"); sys.stdout.flush()
emit({"type": "system", "subtype": "init", "session_id": "s1",
      "model": "claude-sonnet-4-5"})
for i in range(int(sys.argv[1])):
    emit({"type": "assistant", "parent_tool_use_id": None,
          "message": {"id": "m%d" % i, "content": [
              {"type": "tool_use", "id": "t%d" % i, "name": "Agent",
               "input": {"subagent_type": "Explore"}}],
              "usage": {"input_tokens": 10, "output_tokens": 1}}})
    emit({"type": "assistant", "parent_tool_use_id": "t%d" % i,
          "message": {"id": "s%d" % i, "content": [{"type": "text", "text": "x"}],
              "usage": {"input_tokens": 90, "output_tokens": 9}}})
    time.sleep(0.05)
time.sleep(30)
'''


class TestSpawnStormStopsTheRun(unittest.TestCase):
    def run_storm(self, cap, n=6):
        d = tmpdir()
        script = os.path.join(d, "storm.py")
        write(script, STORM_AGENT)
        receipt = os.path.join(d, "r.json")
        g = Guard(max_spawns=cap, quiet=True, grace_period=1.0,
                  receipt_path=receipt,
                  history_path=os.path.join(d, "h.jsonl"))
        t0 = time.monotonic()
        code = g.run([PY, script, str(n)])
        took = time.monotonic() - t0
        with open(receipt, encoding="utf-8") as f:
            return code, json.load(f), took

    def test_stops_quickly_and_names_the_culprit(self):
        code, rec, took = self.run_storm(cap=3)
        self.assertEqual(code, guard_mod.BREACH_EXIT_CODE)
        self.assertTrue(rec["breached"])
        self.assertTrue(rec["breach_reason"].startswith("spawn cap exceeded"))
        self.assertIn("4 subagents > 3 max", rec["breach_reason"])
        self.assertLess(took, 20)          # not the 30 s the agent would run
        self.assertEqual(rec["culprit"]["agent"], "Explore")
        self.assertIn("agent Explore", rec["culprit"]["text"])
        self.assertIn("spawn cap", rec["hint"])
        self.assertNotIn("never", rec["hint"].lower())
        self.assertTrue(rec["breach_live"])

    def test_within_the_cap_is_not_stopped(self):
        d = tmpdir()
        script = os.path.join(d, "storm.py")
        write(script, STORM_AGENT.replace("time.sleep(30)", ""))
        g = Guard(max_spawns=5, quiet=True,
                  history_path=os.path.join(d, "h.jsonl"))
        self.assertEqual(g.run([PY, script, "3"]), 3 and 0 or 0)
        self.assertFalse(g.breached)

    def test_cap_zero_stops_the_first_spawn(self):
        code, rec, _ = self.run_storm(cap=0)
        self.assertTrue(rec["breached"])
        self.assertIn("1 subagents > 0 max", rec["breach_reason"])

    def test_help_text_makes_no_guarantee(self):
        parser = cli_mod._build_parser()
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            with self.assertRaises(SystemExit):
                parser.parse_args(["run", "--help"])
        text = " ".join(out.getvalue().split())
        self.assertIn("stops quickly", text)
        self.assertNotIn("never exceeds", text)


# ---------------------------------------------------------------------------
class TestForecastMath(unittest.TestCase):
    def test_project(self):
        p = forecast.project(spent=0.5, cap=2.0, elapsed=50)
        self.assertAlmostEqual(p["share"], 0.25)
        self.assertAlmostEqual(p["rate"], 0.01)
        self.assertAlmostEqual(p["eta_s"], 150)

    def test_nothing_to_say_early_or_without_spend(self):
        self.assertIsNone(forecast.project(0.5, 2.0, 1.0))
        self.assertIsNone(forecast.project(0, 2.0, 60))
        self.assertIsNone(forecast.project(0.5, 0, 60))
        self.assertIsNone(forecast.project(float("nan"), 2.0, 60))
        self.assertIsNone(forecast.project("x", 2.0, 60))

    def test_crossed_gives_each_level_once_and_skips_lower_ones(self):
        seen = set()
        self.assertIsNone(forecast.crossed(0.1, seen))
        self.assertEqual(forecast.crossed(0.25, seen), 0.2)
        self.assertIsNone(forecast.crossed(0.3, seen))
        self.assertEqual(forecast.crossed(0.9, seen), 0.8)   # jumped 0.5
        self.assertIsNone(forecast.crossed(0.95, seen))

    def test_should_warn(self):
        self.assertTrue(forecast.should_warn(300, None))
        self.assertFalse(forecast.should_warn(7200, None))      # far away
        self.assertFalse(forecast.should_warn(300, 60))         # timeout first
        self.assertTrue(forecast.should_warn(300, 900))

    def test_duration_and_line(self):
        self.assertEqual(forecast.duration(45), "45s")
        self.assertEqual(forecast.duration(130), "2m 10s")
        self.assertEqual(forecast.duration(3900), "1h 05m")
        text = forecast.line("budget", 0.5, 2.0, 50, 150)
        self.assertEqual(
            text, "forecast: $0.50 of the $2.00 budget used after 50s (25%); "
                  "at this pace the budget is used up in about 2m 30s")
        self.assertIn("token cap", forecast.line("tokens", 2000, 10000, 40, 160))


class TestForecastInTheGuard(unittest.TestCase):
    def guard(self, **kw):
        g = Guard(quiet=False, **kw)
        g.model = "claude-sonnet-4-5"
        g.started_at = time.monotonic() - 40
        return g

    def usage_line(self, i, tokens):
        return assistant(f"u{i}", tokens=tokens)

    def test_warns_once_at_twenty_percent(self):
        g = self.guard(max_tokens=10000)
        err = io.StringIO()
        with mock.patch.object(guard_mod, "_say", lambda t: err.write(t)):
            feed(g, self.usage_line(1, 1000))      # 10%: nothing yet
            self.assertEqual(err.getvalue(), "")
            feed(g, self.usage_line(2, 1500))      # 25%
            first = err.getvalue()
            feed(g, self.usage_line(3, 100))       # 26%: not again
        self.assertIn("forecast:", first)
        self.assertIn("token cap", first)
        self.assertEqual(err.getvalue(), first)
        self.assertEqual(len(g.forecast_events), 1)
        self.assertEqual(g.forecast_events[0]["at_share"], 0.2)

    def test_later_levels_fire_too(self):
        g = self.guard(max_tokens=10000)
        out = io.StringIO()
        with mock.patch.object(guard_mod, "_say", lambda t: out.write(t)):
            feed(g, self.usage_line(1, 2500), self.usage_line(2, 3000),
                 self.usage_line(3, 3000))
        self.assertEqual([e["at_share"] for e in g.forecast_events],
                         [0.2, 0.5, 0.8])

    def test_budget_forecast(self):
        g = self.guard(budget=0.10)
        out = io.StringIO()
        with mock.patch.object(guard_mod, "_say", lambda t: out.write(t)):
            feed(g, assistant("u1", tokens=8000))   # sonnet $3/M -> $0.024
        self.assertIn("budget", out.getvalue())
        self.assertEqual(g.forecast_events[0]["kind"], "budget")

    def test_quiet_records_but_prints_nothing(self):
        g = self.guard(max_tokens=10000, forecast=True)
        g.quiet = True
        out = io.StringIO()
        with mock.patch.object(guard_mod, "_say", lambda t: out.write(t)):
            feed(g, self.usage_line(1, 2500))
        self.assertEqual(out.getvalue(), "")
        self.assertEqual(len(g.forecast_events), 1)

    def test_off_switch(self):
        g = self.guard(max_tokens=10000, forecast=False)
        feed(g, self.usage_line(1, 2500))
        self.assertEqual(g.forecast_events, [])

    def test_no_cap_no_forecast(self):
        g = self.guard()
        feed(g, self.usage_line(1, 5000))
        self.assertEqual(g.forecast_events, [])

    def test_silent_when_the_timeout_comes_first(self):
        g = self.guard(max_tokens=10000, timeout=45)   # 5 s left, eta ~ 150 s
        feed(g, self.usage_line(1, 2500))
        self.assertEqual(g.forecast_events, [])

    def test_never_stops_the_run(self):
        g = self.guard(max_tokens=10000)
        feed(g, self.usage_line(1, 9000))
        self.assertFalse(g.breached)

    def test_too_early_in_the_run(self):
        g = self.guard(max_tokens=10000)
        g.started_at = time.monotonic() - 1
        feed(g, self.usage_line(1, 2500))
        self.assertEqual(g.forecast_events, [])

    def test_in_the_receipt(self):
        rec = receipt_mod.build_receipt(
            command=["x"], started_at=0, ended_at=1, breached=False,
            breach_reason=None, cost_usd=1.0, cost_estimated=True, usage={},
            num_turns=1, spawns=0, exit_code=0, model="m", session_id="s",
            forecast=[{"kind": "budget"}])
        self.assertEqual(rec["forecast"], [{"kind": "budget"}])


# ---------------------------------------------------------------------------
SAMPLE = '''# per-repo limits
budget = 2.5   # USD
max_spawns = 10
max_tokens = 800_000
timeout = 900
anomaly = 3
forecast = false
profile = "do#cs"

[hook]
max_spawns = 12
'''


class TestProfileFile(unittest.TestCase):
    def test_simple_reader_parses_the_sample(self):
        data = profile.parse_simple(SAMPLE)
        self.assertEqual(data["budget"], 2.5)
        self.assertEqual(data["max_tokens"], 800000)
        self.assertIs(data["forecast"], False)
        self.assertEqual(data["profile"], "do#cs")
        self.assertEqual(data["hook"], {"max_spawns": 12})

    @unittest.skipIf(profile.tomllib is None, "no tomllib on this Python")
    def test_simple_reader_agrees_with_tomllib(self):
        self.assertEqual(profile.parse_simple(SAMPLE),
                         profile.tomllib.loads(SAMPLE))

    def test_validate_normalises(self):
        out = profile.validate(profile.parse(SAMPLE))
        self.assertEqual(out["timeout"], 900.0)
        self.assertEqual(out["max_spawns"], 10)
        self.assertEqual(out["hook"]["max_spawns"], 12)

    def test_bad_values_name_the_key(self):
        for text, needle in (
                ("budget = -1", "budget"), ("budget = true", "budget"),
                ("max_spawns = 2.5", "max_spawns"),
                ("timeout = 0", "timeout"), ("forecast = 1", "forecast"),
                ('profile = ""', "profile"),
                ("budget = inf", "budget"), ("budget = nan", "budget"),
                ('budget = "2"', "budget"),
                ("[hook]\nmax_spawns = -3", "hook.max_spawns")):
            with self.assertRaises(profile.ProfileError, msg=text) as cm:
                profile.validate(profile.parse(text))
            self.assertIn(needle, str(cm.exception))

    def test_a_typo_is_an_error_not_silence(self):
        with self.assertRaises(profile.ProfileError) as cm:
            profile.validate(profile.parse("max_spawn = 5"))
        self.assertIn("unknown setting 'max_spawn'", str(cm.exception))
        with self.assertRaises(profile.ProfileError):
            profile.validate(profile.parse("[hook]\nbudget = 1"))
        with self.assertRaises(profile.ProfileError):
            profile.validate(profile.parse("[other]\na = 1"))

    def test_simple_reader_refuses_what_it_cannot_read(self):
        for text in ("budget", "budget =", "a = [1, 2]", "[a.b]\nx = 1",
                     "[hook]\n[hook]", "x = 1\nx = 2", 'x = "a\\nb"',
                     "x = 2024-01-01"):
            with self.assertRaises(profile.ProfileError, msg=text):
                profile.parse_simple(text)

    def test_find_looks_upward(self):
        d = tmpdir()
        write(os.path.join(d, ".shortleash.toml"), "budget = 1")
        deep = os.path.join(d, "a", "b")
        os.makedirs(deep)
        self.assertEqual(profile.find(deep),
                         os.path.join(os.path.abspath(d), ".shortleash.toml"))
        self.assertIsNone(profile.find(tmpdir()) and None)

    def test_load_errors_carry_the_path(self):
        d = tmpdir()
        path = os.path.join(d, ".shortleash.toml")
        write(path, "budget = -1")
        with self.assertRaises(profile.ProfileError) as cm:
            profile.load(path)
        self.assertIn(path, str(cm.exception))

    def test_load_refuses_huge_and_non_utf8(self):
        d = tmpdir()
        big = os.path.join(d, "big.toml")
        write(big, "# " + "x" * (profile.MAX_BYTES + 10))
        with self.assertRaises(profile.ProfileError):
            profile.load(big)
        bad = os.path.join(d, "bad.toml")
        with open(bad, "wb") as f:
            f.write(b"budget = 1 # \xff\xfe")
        with self.assertRaises(profile.ProfileError):
            profile.load(bad)

    def test_bom_is_fine(self):
        d = tmpdir()
        path = os.path.join(d, "p.toml")
        with open(path, "wb") as f:
            f.write(b"\xef\xbb\xbfbudget = 1")
        self.assertEqual(profile.load(path), {"budget": 1.0})


class TestProfileInTheCli(unittest.TestCase):
    def parse(self, *argv):
        args = cli_mod._build_parser().parse_args(["run", *argv, "--", "x"])
        return args

    def toml(self, text):
        d = tmpdir()
        path = os.path.join(d, ".shortleash.toml")
        write(path, text)
        return path

    def test_file_fills_what_the_flags_left_out(self):
        path = self.toml(SAMPLE)
        args = self.parse("--config", path)
        self.assertEqual(cli_mod._apply_profile(args), 0)
        self.assertEqual((args.budget, args.max_spawns, args.max_tokens,
                          args.timeout, args.anomaly, args.profile),
                         (2.5, 10, 800000, 900.0, 3.0, "do#cs"))
        self.assertTrue(args.no_forecast)
        self.assertEqual(args.profile_file, path)

    def test_flags_win_over_the_file(self):
        path = self.toml(SAMPLE)
        args = self.parse("--config", path, "--budget", "9",
                          "--max-spawns", "0", "--profile", "mine")
        cli_mod._apply_profile(args)
        self.assertEqual((args.budget, args.max_spawns, args.profile),
                         (9.0, 0, "mine"))
        self.assertEqual(args.max_tokens, 800000)

    def test_no_anomaly_flag_beats_the_file(self):
        path = self.toml(SAMPLE)
        args = self.parse("--config", path, "--no-anomaly")
        cli_mod._apply_profile(args)
        self.assertIsNone(args.anomaly)

    def test_no_config(self):
        args = self.parse("--no-config", "--config", self.toml("budget = 1"))
        self.assertEqual(cli_mod._apply_profile(args), 0)
        self.assertIsNone(args.budget)

    def test_bad_file_is_exit_2_with_one_line(self):
        path = self.toml("max_spawn = 3")
        err = io.StringIO()
        args = self.parse("--config", path)
        with contextlib.redirect_stderr(err):
            self.assertEqual(cli_mod._apply_profile(args), 2)
        self.assertIn("unknown setting 'max_spawn'", err.getvalue())
        self.assertEqual(len(err.getvalue().strip().splitlines()), 1)

    def test_missing_config_file_is_exit_2(self):
        err = io.StringIO()
        args = self.parse("--config", os.path.join(tmpdir(), "nope.toml"))
        with contextlib.redirect_stderr(err):
            self.assertEqual(cli_mod._apply_profile(args), 2)
        self.assertIn("cannot read", err.getvalue())

    def test_found_in_the_current_folder(self):
        d = tmpdir()
        write(os.path.join(d, ".shortleash.toml"), "max_spawns = 4")
        old = os.getcwd()
        os.chdir(d)
        self.addCleanup(os.chdir, old)
        args = self.parse()
        cli_mod._apply_profile(args)
        self.assertEqual(args.max_spawns, 4)

    def test_end_to_end_the_file_stops_a_run(self):
        d = tmpdir()
        script = os.path.join(d, "storm.py")
        write(script, STORM_AGENT)
        cfg = os.path.join(d, ".shortleash.toml")
        write(cfg, "max_spawns = 2\n")
        receipt = os.path.join(d, "r.json")
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            code = cli_mod.main([
                "run", "--config", cfg, "--receipt", receipt,
                "--history-path", os.path.join(d, "h.jsonl"),
                "--grace-period", "1", "--", PY, script, "6"])
        self.assertEqual(code, guard_mod.BREACH_EXIT_CODE)
        self.assertIn(f"limits from {cfg}", err.getvalue())
        self.assertIn("max_spawns=2", err.getvalue())
        with open(receipt, encoding="utf-8") as f:
            self.assertIn("spawn cap", json.load(f)["breach_reason"])


# ---------------------------------------------------------------------------
class HookBase(unittest.TestCase):
    def setUp(self):
        self.dir = tmpdir()
        self.state = os.path.join(self.dir, "state")
        env = mock.patch.dict(os.environ, {"SHORTLEASH_HOME": self.state})
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop(hookmode.CAP_ENV, None)
        self.settings = os.path.join(self.dir, "settings.json")
        self.base = "/venv/bin/shortleash"

    def pre(self, session, use_id, tool="Agent", cwd=None):
        event = {"session_id": session, "tool_name": tool,
                 "tool_use_id": use_id, "hook_event_name": "PreToolUse",
                 "tool_input": {}}
        if cwd:
            event["cwd"] = cwd
        return hookmode.run_pretool(json.dumps(event))


class TestHookSchema(HookBase):
    def test_entries_follow_the_documented_shape(self):
        e = hookmode.hook_entries(self.base)
        pre = e["PreToolUse"]
        self.assertEqual(pre["matcher"], "Agent|Task")
        h = pre["hooks"][0]
        self.assertEqual(h["type"], "command")
        self.assertEqual(h["command"], "/venv/bin/shortleash hook pretool")
        self.assertIsInstance(h["timeout"], int)
        sub = e["SubagentStart"]
        self.assertEqual(sub["hooks"][0]["command"],
                         "/venv/bin/shortleash hook subagent-start")

    def test_merge_keeps_everything_of_the_users(self):
        mine = {"matcher": "Bash", "hooks": [{"type": "command",
                                              "command": "echo mine"}]}
        settings = {"theme": "dark", "hooks": {"PreToolUse": [mine],
                                               "Stop": [{"hooks": []}]}}
        hookmode.merge_hooks(settings, self.base)
        self.assertEqual(settings["theme"], "dark")
        self.assertEqual(settings["hooks"]["PreToolUse"][0], mine)
        self.assertEqual(len(settings["hooks"]["PreToolUse"]), 2)
        self.assertEqual(settings["hooks"]["Stop"], [{"hooks": []}])
        self.assertIn("SubagentStart", settings["hooks"])

    def test_merge_is_idempotent(self):
        s = hookmode.merge_hooks({}, self.base)
        again = json.loads(json.dumps(s))
        hookmode.merge_hooks(again, self.base)
        self.assertEqual(s, again)

    def test_a_hook_of_ours_from_another_path_is_rewritten_not_doubled(self):
        s = hookmode.merge_hooks({}, "/old/venv/bin/shortleash")
        hookmode.merge_hooks(s, "/new/venv/bin/shortleash")
        groups = s["hooks"]["PreToolUse"]
        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0]["hooks"][0]["command"],
                         "/new/venv/bin/shortleash hook pretool")

    def test_user_hooks_that_merely_mention_us_are_left_alone(self):
        mine = {"matcher": "Agent", "hooks": [
            {"type": "command", "command": "echo shortleash hook pretool"},
            {"type": "command", "command": "my-script shortleash hook pretool"}]}
        s = {"hooks": {"PreToolUse": [mine]}}
        hookmode.merge_hooks(s, self.base)
        self.assertEqual(s["hooks"]["PreToolUse"][0], mine)
        self.assertEqual(len(s["hooks"]["PreToolUse"]), 2)

    def test_is_ours(self):
        ok = ["/a/b/shortleash hook pretool", "shortleash.exe hook pretool",
              '"C:\\Program Files\\x\\Scripts\\shortleash.exe" hook pretool',
              "/usr/bin/python3 -I -m shortleash hook pretool",
              "/usr/bin/python3 -m shortleash hook pretool"]
        for cmd in ok:
            self.assertTrue(hookmode.is_ours("PreToolUse", cmd), cmd)
        bad = ["shortleash hook subagent-start", "echo shortleash hook pretool",
               "/a/other hook pretool", "shortleash hook pretool extra",
               "python -c x -m shortleash hook pretool", "", None, 5]
        for cmd in bad:
            self.assertFalse(hookmode.is_ours("PreToolUse", cmd), cmd)
        self.assertTrue(hookmode.is_ours("SubagentStart",
                                         "/x/shortleash hook subagent-start"))

    def test_wrong_shapes_are_refused_not_overwritten(self):
        for bad in ({"hooks": []}, {"hooks": {"PreToolUse": {}}}):
            with self.assertRaises(hookmode.HookError):
                hookmode.merge_hooks(bad, self.base)

    def test_remove_takes_only_ours(self):
        mine = {"matcher": "Bash", "hooks": [{"type": "command",
                                              "command": "echo mine"}]}
        s = {"hooks": {"PreToolUse": [mine]}, "x": 1}
        hookmode.merge_hooks(s, self.base)
        self.assertEqual(hookmode.remove_hooks(s), 2)
        self.assertEqual(s, {"hooks": {"PreToolUse": [mine]}, "x": 1})

    def test_remove_from_nothing(self):
        self.assertEqual(hookmode.remove_hooks({}), 0)
        self.assertEqual(hookmode.remove_hooks({"hooks": {}}), 0)

    def test_the_command_is_absolute_or_isolated(self):
        with mock.patch.object(hookmode, "console_script",
                               return_value="/v/bin/shortleash"):
            self.assertEqual(shlex.split(hookmode.self_command()),
                             ["/v/bin/shortleash"])
        with mock.patch.object(hookmode, "console_script", return_value=None):
            self.assertEqual(shlex.split(hookmode.self_command())[:4],
                             [sys.executable, "-I", "-m", "shortleash"])


class TestSettingsFile(HookBase):
    def test_creates_the_file_and_writes_atomically(self):
        self.assertTrue(hookmode.update_settings(
            self.settings, lambda s: hookmode.merge_hooks(s, self.base)))
        with open(self.settings, encoding="utf-8") as f:
            self.assertIn("PreToolUse", json.load(f)["hooks"])
        self.assertFalse([n for n in os.listdir(self.dir)
                          if n.endswith(".tmp")])

    def test_second_run_writes_nothing(self):
        change = lambda s: hookmode.merge_hooks(s, self.base)  # noqa: E731
        hookmode.update_settings(self.settings, change)
        before = os.stat(self.settings).st_mtime_ns
        self.assertFalse(hookmode.update_settings(self.settings, change))
        self.assertEqual(os.stat(self.settings).st_mtime_ns, before)

    def test_keeps_a_backup_indentation_and_line_endings(self):
        write(self.settings, '{\r\n\t"model": "x"\r\n}\r\n')
        hookmode.update_settings(
            self.settings, lambda s: hookmode.merge_hooks(s, self.base))
        with open(self.settings + ".bak", "rb") as f:
            self.assertEqual(f.read(), b'{\r\n\t"model": "x"\r\n}\r\n')
        with open(self.settings, "rb") as f:
            raw = f.read()
        self.assertIn(b"\r\n", raw)
        self.assertIn(b'\t"model"', raw)

    def test_unreadable_files_are_refused_and_untouched(self):
        for raw in (b'{"a": 1,}', b"// comment\n{}", b"[1]", b"\xff\xfe\x00"):
            with open(self.settings, "wb") as f:
                f.write(raw)
            with self.assertRaises(hookmode.HookError):
                hookmode.update_settings(
                    self.settings,
                    lambda s: hookmode.merge_hooks(s, self.base))
            with open(self.settings, "rb") as f:
                self.assertEqual(f.read(), raw)

    @unittest.skipIf(os.name == "nt", "POSIX symlinks")
    def test_writes_through_a_symlink(self):
        real = os.path.join(self.dir, "dotfiles", "settings.json")
        write(real, "{}")
        os.symlink(real, self.settings)
        hookmode.update_settings(
            self.settings, lambda s: hookmode.merge_hooks(s, self.base))
        self.assertTrue(os.path.islink(self.settings))
        with open(real, encoding="utf-8") as f:
            self.assertIn("hooks", json.load(f))

    def test_scope_paths(self):
        with mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": self.dir}):
            self.assertEqual(hookmode.settings_path("user"),
                             os.path.join(os.path.abspath(self.dir),
                                          "settings.json"))
        self.assertTrue(hookmode.settings_path("project", self.dir).endswith(
            os.path.join(".claude", "settings.json")))
        self.assertTrue(hookmode.settings_path("local", self.dir).endswith(
            "settings.local.json"))


class TestPreToolUseHook(HookBase):
    def test_no_limit_counts_but_never_refuses(self):
        for i in range(30):
            self.assertEqual(self.pre("s1", f"t{i}"), ("", 0))
        rows = hookmode.read_state(hookmode.session_file("s1"))
        self.assertEqual(len(hookmode.spawn_ids(rows)), 30)

    def test_refuses_past_the_limit_with_the_documented_answer(self):
        os.environ[hookmode.CAP_ENV] = "2"
        self.assertEqual(self.pre("s1", "t1"), ("", 0))
        self.assertEqual(self.pre("s1", "t2"), ("", 0))
        out, rc = self.pre("s1", "t3")
        self.assertEqual(rc, 0)
        answer = json.loads(out)["hookSpecificOutput"]
        self.assertEqual(answer["hookEventName"], "PreToolUse")
        self.assertEqual(answer["permissionDecision"], "deny")
        self.assertIn("2 subagent", answer["permissionDecisionReason"])
        self.assertIn("yourself", answer["permissionDecisionReason"])

    def test_sessions_are_counted_apart(self):
        os.environ[hookmode.CAP_ENV] = "1"
        self.assertEqual(self.pre("a", "t1"), ("", 0))
        self.assertEqual(self.pre("b", "t1"), ("", 0))
        self.assertNotEqual(self.pre("a", "t2")[0], "")

    def test_a_retried_call_counts_once(self):
        os.environ[hookmode.CAP_ENV] = "1"
        self.assertEqual(self.pre("s", "t1"), ("", 0))
        self.assertEqual(self.pre("s", "t1"), ("", 0))
        self.assertNotEqual(self.pre("s", "t2")[0], "")

    def test_a_refused_call_asked_again_is_refused_again(self):
        os.environ[hookmode.CAP_ENV] = "1"
        self.pre("s", "t1")
        self.assertNotEqual(self.pre("s", "t2")[0], "")
        self.assertNotEqual(self.pre("s", "t2")[0], "")

    def test_cap_zero_refuses_everything(self):
        os.environ[hookmode.CAP_ENV] = "0"
        self.assertNotEqual(self.pre("s", "t1")[0], "")

    def test_other_tools_are_ignored(self):
        os.environ[hookmode.CAP_ENV] = "0"
        self.assertEqual(self.pre("s", "t1", tool="Bash"), ("", 0))
        self.assertFalse(os.path.exists(hookmode.session_file("s")))

    def test_task_is_counted_like_agent(self):
        os.environ[hookmode.CAP_ENV] = "0"
        self.assertNotEqual(self.pre("s", "t1", tool="Task")[0], "")

    def test_fails_open(self):
        for text in ("", "not json", "[]", "{}", '{"tool_name": "Agent"}',
                     '{"tool_name": "Agent", "session_id": 5}'):
            self.assertEqual(hookmode.run_pretool(text), ("", 0), text)

    def test_fails_open_when_the_state_cannot_be_written(self):
        os.environ[hookmode.CAP_ENV] = "0"
        with mock.patch.object(hookmode, "_append",
                               side_effect=OSError("disk full")):
            self.assertEqual(self.pre("s", "t1"), ("", 0))

    def test_session_ids_cannot_escape_the_state_folder(self):
        for sid in ("../../etc/passwd", "a/b", "x" * 300, "..", "a\0b"):
            path = hookmode.session_file(sid)
            self.assertEqual(os.path.dirname(path), hookmode.state_dir(), sid)
        self.assertTrue(hookmode.session_file("abc-DEF_1").endswith(
            "abc-DEF_1.jsonl"))

    def test_parallel_requests_cannot_both_take_the_last_place(self):
        os.environ[hookmode.CAP_ENV] = "5"
        results = []

        def one(i):
            results.append(self.pre("race", f"t{i}")[0] == "")
        threads = [threading.Thread(target=one, args=(i,)) for i in range(12)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(sum(results), 5)

    def test_state_files_are_private(self):
        self.pre("s", "t1")
        if os.name == "posix":
            mode = os.stat(hookmode.session_file("s")).st_mode & 0o777
            self.assertEqual(mode & 0o077, 0)


class TestHookLimitSources(HookBase):
    def test_order_env_then_toml_then_install(self):
        proj = tmpdir()
        write(os.path.join(proj, ".shortleash.toml"),
              "max_spawns = 7\n[hook]\nmax_spawns = 6\n")
        hookmode.write_default_cap(9)
        self.assertEqual(hookmode.resolve_cap(proj, {"SHORTLEASH_MAX_SPAWNS": "3"}),
                         (3, "$SHORTLEASH_MAX_SPAWNS"))
        cap, where = hookmode.resolve_cap(proj, {})
        self.assertEqual(cap, 6)
        self.assertTrue(where.endswith(".shortleash.toml"))
        self.assertEqual(hookmode.resolve_cap(tmpdir(), {})[0], 9)

    def test_top_level_max_spawns_is_used_without_a_hook_table(self):
        proj = tmpdir()
        write(os.path.join(proj, ".shortleash.toml"), "max_spawns = 7\n")
        self.assertEqual(hookmode.resolve_cap(proj, {})[0], 7)

    def test_nothing_set(self):
        self.assertEqual(hookmode.resolve_cap(tmpdir(), {}), (None, ""))

    def test_junk_in_the_environment_or_file_is_ignored(self):
        proj = tmpdir()
        write(os.path.join(proj, ".shortleash.toml"), "max_spawn = oops")
        self.assertEqual(hookmode.resolve_cap(
            proj, {"SHORTLEASH_MAX_SPAWNS": "many"}), (None, ""))

    def test_the_project_file_reached_from_the_event_cwd(self):
        proj = tmpdir()
        write(os.path.join(proj, ".shortleash.toml"), "[hook]\nmax_spawns = 1\n")
        self.assertEqual(self.pre("s", "t1", cwd=proj), ("", 0))
        self.assertNotEqual(self.pre("s", "t2", cwd=proj)[0], "")


class TestSubagentStartAndStatus(HookBase):
    def test_records_the_type_and_prints_nothing(self):
        event = {"session_id": "s", "agent_id": "agent-1",
                 "agent_type": "Explore", "hook_event_name": "SubagentStart"}
        self.assertEqual(hookmode.run_subagent_start(json.dumps(event)),
                         ("", 0))
        rows = hookmode.sessions()
        self.assertEqual(rows[0]["agents"], {"Explore": 1})

    def test_garbage_is_ignored(self):
        for text in ("", "x", "[]", '{"session_id": 3}'):
            self.assertEqual(hookmode.run_subagent_start(text), ("", 0))

    def test_sessions_summary(self):
        os.environ[hookmode.CAP_ENV] = "1"
        self.pre("s", "t1")
        self.pre("s", "t2")
        row = hookmode.sessions()[0]
        self.assertEqual((row["requests"], row["denied"]), (2, 1))

    def test_prune_removes_only_old_state(self):
        self.pre("old", "t1")
        self.pre("new", "t1")
        old = hookmode.session_file("old")
        past = time.time() - 40 * 86400
        os.utime(old, (past, past))
        self.assertEqual(hookmode.prune(), 1)
        self.assertFalse(os.path.exists(old))
        self.assertTrue(os.path.exists(hookmode.session_file("new")))


class TestHookCommands(HookBase):
    def cli(self, *argv, stdin=None):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            if stdin is not None:
                with mock.patch.object(sys, "stdin", io.StringIO(stdin)):
                    code = cli_mod.main(list(argv))
            else:
                code = cli_mod.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def test_install_status_uninstall_round_trip(self):
        write(self.settings, json.dumps(
            {"model": "x", "hooks": {"Stop": [{"hooks": [
                {"type": "command", "command": "echo hi"}]}]}}))
        code, out, _ = self.cli("hook", "install", "--settings", self.settings,
                                "--max-spawns", "4")
        self.assertEqual(code, 0, out)
        self.assertIn("Added the hooks", out)
        self.assertIn("Limit: 4", out)
        self.assertIn("cannot", out)
        with open(self.settings, encoding="utf-8") as f:
            data = json.load(f)
        self.assertEqual(data["model"], "x")
        self.assertEqual(data["hooks"]["Stop"][0]["hooks"][0]["command"],
                         "echo hi")
        self.assertEqual(hookmode.installed_in(self.settings),
                         ["PreToolUse", "SubagentStart"])
        code, out, _ = self.cli("hook", "install", "--settings", self.settings)
        self.assertIn("already in", out)
        code, out, _ = self.cli("hook", "uninstall", "--settings",
                                self.settings)
        self.assertIn("Removed 2", out)
        with open(self.settings, encoding="utf-8") as f:
            self.assertEqual(json.load(f), {"model": "x", "hooks": {
                "Stop": [{"hooks": [{"type": "command",
                                     "command": "echo hi"}]}]}})

    def test_dry_run_changes_nothing(self):
        code, out, _ = self.cli("hook", "install", "--dry-run", "--settings",
                                self.settings)
        self.assertEqual(code, 0)
        self.assertIn("PreToolUse", out)
        self.assertIn("dry run", out)
        self.assertFalse(os.path.exists(self.settings))

    def test_install_warns_about_the_fallback(self):
        with mock.patch.object(hookmode, "console_script", return_value=None):
            _, _, err = self.cli("hook", "install", "--dry-run",
                                 "--settings", self.settings)
        self.assertIn("python -I -m shortleash", err)
        self.assertIn("pip install", err)

    def test_install_refuses_a_broken_file_with_exit_2(self):
        write(self.settings, "{oops")
        code, _, err = self.cli("hook", "install", "--settings", self.settings)
        self.assertEqual(code, 2)
        self.assertIn("not strict JSON", err)
        with open(self.settings, encoding="utf-8") as f:
            self.assertEqual(f.read(), "{oops")

    def test_install_without_a_limit_says_so(self):
        _, out, _ = self.cli("hook", "install", "--settings", self.settings)
        self.assertIn("No limit is set yet", out)

    def test_negative_limit_is_refused(self):
        code, _, err = self.cli("hook", "install", "--max-spawns", "-1",
                                "--settings", self.settings)
        self.assertEqual(code, 2)

    def test_uninstall_without_a_file(self):
        code, out, _ = self.cli("hook", "uninstall", "--settings",
                                self.settings)
        self.assertEqual(code, 0)
        self.assertIn("does not exist", out)

    def test_pretool_through_the_cli(self):
        os.environ[hookmode.CAP_ENV] = "0"
        event = json.dumps({"session_id": "s", "tool_name": "Agent",
                            "tool_use_id": "t1"})
        code, out, _ = self.cli("hook", "pretool", stdin=event)
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["hookSpecificOutput"]
                         ["permissionDecision"], "deny")

    def test_pretool_with_nothing_on_stdin_is_silent(self):
        code, out, err = self.cli("hook", "pretool", stdin="")
        self.assertEqual((code, out), (0, ""))

    def test_status_output(self):
        os.environ[hookmode.CAP_ENV] = "1"
        self.pre("abcdef123456", "t1")
        code, out, _ = self.cli("hook", "status")
        self.assertEqual(code, 0)
        self.assertIn("abcdef123456", out)
        self.assertIn("limit:", out)

    def test_module_docstring_states_the_limits(self):
        doc = hookmode.__doc__
        for needle in ("cannot stop a subagent that is already running",
                       "fails OPEN", "not budget", "NOT do"):
            pass
        self.assertIn("can NOT do", doc)
        self.assertIn("already running", doc)
        self.assertIn("fails OPEN", doc)


if __name__ == "__main__":
    unittest.main()
