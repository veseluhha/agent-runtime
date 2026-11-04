import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from .client import run_suite
from .http_api import Fault


class SuiteTest(unittest.TestCase):
    def test_failed_task_does_not_hide_following_results(self):
        ids = ["B1.1", "B2.1", "B3.1"]

        def reply(url, path, body, **kwargs):
            if path == "/v0/tasks":
                return {"task_ids": ids}
            task_id = body["task_id"]
            if task_id == "B2.1":
                raise Fault("timeout", "deadline", 504)
            return {"task_id": task_id, "pass": True, "reason": "ok", "seconds": 1}

        with tempfile.TemporaryDirectory() as tmp, patch("runtime.client.post", reply), redirect_stdout(io.StringIO()):
            self.assertEqual(run_suite("", "token", folder=Path(tmp)), 1)
            log, = Path(tmp).glob("*.jsonl")
            rows = [json.loads(line) for line in log.read_text().splitlines()]
        self.assertEqual([row["task_id"] for row in rows], ids)
        self.assertEqual([row["pass"] for row in rows], [True, False, True])
        self.assertEqual(rows[1]["outcome"], "timeout")

    def test_checkpoint_requires_every_planned_task(self):
        with patch("runtime.client.post", return_value={"task_ids": ["B1.1"]}):
            with self.assertRaisesRegex(SystemExit, "B2.1"):
                run_suite("", "token", required=("B1.1", "B2.1"))
