import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from .harness import restore, run_smoke
from .http_api import Fault, post
from .tasks import TASKS


def check(task_id, name, content):
    with tempfile.TemporaryDirectory() as tmp:
        (Path(tmp) / name).write_bytes(content)
        output = subprocess.check_output([sys.executable, str(TASKS[task_id].directory / "check.py"), tmp])
    return json.loads(output)["pass"]


class SmokeControls(unittest.TestCase):
    def test_mock_uses_observed_port(self):
        body = {"model": "scripted-b1", "messages": [
            {"role": "user", "task": "read port"},
            {"role": "tool", "output": "  port: 4917\n  # old port: 8080\n"}]}
        response = post("http://llm-proxy:8080", "/v0/next", body)
        self.assertEqual(response["argv"][-1].strip(), "4917")

    def test_checkers_reject_wrong_answers(self):
        readme = (TASKS["B4.1"].directory / "fixture/README.md").read_bytes()
        for task_id, name, wrong in [
            ("B1.1", "answer.txt", b"8080"),
            ("B2.1", "answer.txt", b"src/report.py"),
            ("B3.1", "config.json", b'{"service":"orders","retries":"3","debug":false}'),
            ("B4.1", "README.md", readme.replace(b"==1.2", b"==1.3")),
            ("B5.1", "answer.txt", b"16"),
        ]:
            with self.subTest(task_id=task_id):
                self.assertFalse(check(task_id, name, wrong))

    def test_collector_rejects_parent_path(self):
        with tempfile.TemporaryDirectory() as tmp, self.assertRaises(Fault):
            restore({"../escape": "YWJj"}, Path(tmp))

    def test_agent_can_reason_about_a_failed_command(self):
        observations = []

        def next_action(base, path, body, **kwargs):
            if path != "/v0/next":
                return post(base, path, body, **kwargs)
            history = body["messages"]
            if history[-1]["role"] == "user":
                return {"argv": ["sh", "-c", "echo OK; echo build-failed >&2; exit 1"]}
            observations.append(history[-1])
            return {"done": True}

        with patch("runtime.harness.post", next_action):
            result = run_smoke(TASKS["B1.1"])
        self.assertEqual(len(observations), 1)
        self.assertEqual(observations[0]["exit_code"], 1)
        self.assertCountEqual(observations[0]["output"].splitlines(), ["OK", "build-failed"])
        self.assertEqual(result["outcome"], "completed")
        self.assertFalse(result["pass"])  # no answer.txt
