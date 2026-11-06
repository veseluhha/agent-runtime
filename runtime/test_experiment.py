import io
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from .completions import Conversation
from .experiment import record_turn, replay_turn, run
from .http_api import Fault
from .model_proxy import dispatch


class RecordingTest(unittest.TestCase):
    def test_missing_key_does_not_fall_back_to_scripted_actions(self):
        with patch.dict("os.environ", {"LLM_MODEL": "gpt-6-astra",
                                      "LLM_BASE_URL": "https://api.openai.com/v1",
                                      "DEEPSEEK_API_KEY": "different-provider-key"}, clear=True):
            with self.assertRaisesRegex(Fault, "LLM_API_KEY") as error:
                dispatch("/v0/model", {}, {})
        self.assertEqual(error.exception.status, 503)

    def test_proxy_forwards_model_tools_and_observations(self):
        payload = {"model": "gpt-6-astra", "messages": [
            {"role": "tool", "tool_call_id": "call-1", "content": "file-not-found"}],
            "tools": [{"type": "function", "function": {"name": "run_command"}}]}
        response = {"choices": [], "model": "gpt-6-astra"}
        with patch.dict("os.environ", {"LLM_MODEL": "gpt-6-astra",
                                      "LLM_BASE_URL": "https://api.openai.com/v1",
                                      "LLM_API_KEY": "test-only"}, clear=True):
            with patch("runtime.model_proxy.build_opener") as opener:
                opener.return_value.open.return_value.__enter__.return_value = io.StringIO(json.dumps(response))
                result = dispatch("/v0/completion", {"request": payload, "timeout_s": 10}, {})
                request = opener.return_value.open.call_args.args[0]
        self.assertEqual(request.full_url, "https://api.openai.com/v1/chat/completions")
        self.assertEqual(json.loads(request.data), payload)
        self.assertEqual(result, response)

    def test_single_task_record_replay_runs_in_sandbox_and_keeps_failed_verdict(self):
        def reply(base, path, body, **kwargs):
            if path == "/v0/model":
                return {"model": "protocol-test"}
            request = body["request"]
            if request["messages"][-1]["role"] == "tool":
                observation = json.loads(request["messages"][-1]["content"])
                self.assertEqual(observation, {"exit_code": 1, "output": "probe-error\n"})
                message = {"role": "assistant", "content": "finished without an answer"}
                reason = "stop"
            else:
                message = {"role": "assistant", "content": None, "tool_calls": [{
                    "id": "probe", "type": "function", "function": {"name": "run_command",
                    "arguments": json.dumps({"argv": ["sh", "-c", "echo probe-error; exit 1"]})}}]}
                reason = "tool_calls"
            return {"model": "protocol-test", "choices": [{"message": message, "finish_reason": reason}]}

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with patch("runtime.experiment.post", side_effect=reply) as provider:
                recorded = run("record", root, task_ids=["B1.1"])
                self.assertEqual(provider.call_count, 3)
            source, = (root / "record").iterdir()
            with patch("runtime.experiment.post", side_effect=AssertionError("replay contacted provider")):
                replayed = run("replay", root, source.name)
            self.assertEqual(recorded[0]["outcome"], "completed")
            self.assertFalse(recorded[0]["pass"])
            self.assertEqual(replayed[0]["outcome"], "completed")
            self.assertFalse(replayed[0]["pass"])
            self.assertIsNone(recorded[0]["tokens_in"])
            self.assertEqual(replayed[0]["tokens_in"], 0)

    def test_record_and_replay_multitool_with_changed_observation(self):
        calls = [{"id": str(i), "type": "function", "function": {
            "name": "run_command", "arguments": json.dumps({"argv": [cmd]})}}
            for i, cmd in enumerate(["pwd", "ls"])]
        first = {"role": "assistant", "content": None, "reasoning_content": "fixture", "tool_calls": calls,
                 "reasoning_details": [{"type": "reasoning.text", "text": "protocol fixture", "index": 0}]}
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
