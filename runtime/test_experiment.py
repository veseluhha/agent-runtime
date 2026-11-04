import io
import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from .completions import Conversation
from .experiment import record_turn, replay_turn
from .http_api import Fault


class RecordingTest(unittest.TestCase):
    def test_record_and_replay_multitool_with_changed_observation(self):
        calls = [{"id": str(i), "type": "function", "function": {
            "name": "run_command", "arguments": json.dumps({"argv": [cmd]})}}
            for i, cmd in enumerate(["pwd", "ls"])]
        first = {"role": "assistant", "content": None, "reasoning_content": "fixture", "tool_calls": calls}
        last = {"role": "assistant", "content": "done"}
        responses = [{"model": "test-id", "usage": {"prompt_tokens": 7, "completion_tokens": 3},
                      "choices": [{"message": msg, "finish_reason": reason}]}
                     for msg, reason in [(first, "tool_calls"), (last, "stop")]]
        task = SimpleNamespace(messages=["Inspect the fixture"])
        history = [{"role": "user", "task": task.messages[0]}]
        observed = history + [{"role": "tool", "output": out, "exit_code": 0} for out in ["/work", "data.csv"]]
        tape = io.StringIO()
        for fn in (record_turn, replay_turn):
            tape.seek(0)
            chat = Conversation("test-id", lambda req, timeout: fn(tape, req, timeout))
            with patch("runtime.experiment.post", side_effect=responses) as provider:
                self.assertEqual(chat.next_action(task, history, 5), {"commands": [["pwd"], ["ls"]]})
                self.assertEqual(chat.next_action(task, observed, 5), {"done": True})
                self.assertEqual(provider.call_count, 2 if fn is record_turn else 0)
            self.assertEqual(chat.tokens("prompt_tokens"), 14)
        second = json.loads(tape.getvalue().splitlines()[1])["request"]["messages"]
        self.assertEqual(second[-3], first)
        self.assertEqual([m["tool_call_id"] for m in second[-2:]], ["0", "1"])
        tape.seek(0)
        replay = Conversation("test-id", lambda req, timeout: replay_turn(tape, req, timeout))
        replay.next_action(task, history, 5)
        with self.assertRaisesRegex(Fault, "differs"):
            replay.next_action(task, observed[:-1] + [{**observed[-1], "exit_code": 1}], 5)

    def test_provider_error_is_saved_and_replayed(self):
        tape = io.StringIO()
        with patch("runtime.experiment.post", side_effect=Fault("provider_http", "provider returned HTTP 429")):
            with self.assertRaisesRegex(Fault, "HTTP 429"):
                record_turn(tape, {"model": "test"}, 5)
        tape.seek(0)
        with self.assertRaisesRegex(Fault, "HTTP 429"):
            replay_turn(tape, {"model": "test"}, 5)
