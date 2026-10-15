import json
import unittest

from shortleash import events


def _assistant_msg(text="hi", tool_uses=0, usage=None):
    content = [{"type": "text", "text": text}]
    for i in range(tool_uses):
        content.append({"type": "tool_use", "id": f"t{i}", "name": "Task",
                        "input": {}})
    return {"type": "assistant",
            "message": {"content": content,
                        "usage": usage or {"input_tokens": 100,
                                           "output_tokens": 50,
                                           "cache_creation_input_tokens": 10,
                                           "cache_read_input_tokens": 200}}}


class TestParseLine(unittest.TestCase):
    def test_init(self):
        obs = events.parse_line(json.dumps(
            {"type": "system", "subtype": "init",
             "session_id": "abc", "model": "claude-sonnet-4-5"}))
        self.assertEqual(obs["kind"], "init")
        self.assertEqual(obs["model"], "claude-sonnet-4-5")
        self.assertEqual(obs["session_id"], "abc")

    def test_assistant_usage_and_spawns(self):
        obs = events.parse_line(json.dumps(_assistant_msg(tool_uses=2)))
        self.assertEqual(obs["kind"], "usage")
        self.assertEqual(obs["usage"]["input"], 100)
        self.assertEqual(obs["usage"]["cache_read"], 200)
        self.assertEqual(obs["spawns"], 2)

    def test_assistant_no_spawns(self):
        obs = events.parse_line(json.dumps(_assistant_msg()))
        self.assertEqual(obs["spawns"], 0)

    def test_non_task_tool_use_not_counted(self):
        msg = _assistant_msg()
        msg["message"]["content"].append(
            {"type": "tool_use", "id": "b1", "name": "Bash", "input": {}})
        obs = events.parse_line(json.dumps(msg))
        self.assertEqual(obs["spawns"], 0)

    def test_result_success(self):
        obs = events.parse_line(json.dumps(
            {"type": "result", "subtype": "success", "is_error": False,
             "total_cost_usd": 1.23, "num_turns": 7,
             "usage": {"input_tokens": 10, "output_tokens": 5}}))
        self.assertEqual(obs["kind"], "result")
        self.assertFalse(obs["is_error"])
        self.assertAlmostEqual(obs["cost_usd"], 1.23)
        self.assertEqual(obs["num_turns"], 7)

    def test_result_error_flag(self):
        # subtype "success" can lie; is_error is authoritative.
        obs = events.parse_line(json.dumps(
            {"type": "result", "subtype": "success", "is_error": True,
             "result": "Not logged in"}))
        self.assertTrue(obs["is_error"])

    def test_hook_noise_ignored(self):
        for subtype in ("hook_started", "hook_response", "api_retry"):
            obs = events.parse_line(json.dumps(
                {"type": "system", "subtype": subtype}))
            self.assertIsNone(obs, subtype)

    def test_non_json_ignored(self):
        self.assertIsNone(events.parse_line("[shortleash] guarding..."))
        self.assertIsNone(events.parse_line(""))
        self.assertIsNone(events.parse_line("   "))
        self.assertIsNone(events.parse_line("not json at all"))

    def test_malformed_json_ignored(self):
        self.assertIsNone(events.parse_line('{"type": "assistant",'))


if __name__ == "__main__":
    unittest.main()
