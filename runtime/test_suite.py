import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from .client import run_suite, send_message
from .http_api import Fault, TransportError


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

    def test_checkpoint_skips_tasks_added_after_it(self):
        responses = [{"task_ids": ["B1.1", "B6.1"]},
                     {"task_id": "B1.1", "pass": True, "seconds": 1, "reason": "ok"}]
        with tempfile.TemporaryDirectory() as tmp, patch("runtime.client.post", side_effect=responses) as calls:
            with redirect_stdout(io.StringIO()):
                self.assertEqual(run_suite("", "token", Path(tmp), required=("B1.1",)), 0)
            self.assertEqual(calls.call_args.args[2], {"task_id": "B1.1"})

    def test_reconnect_resubmits_only_an_unreceived_message(self):
        result = {"answer": "done", "events": []}
        lost = TransportError("transport", "no response", 502)
        replies = [lost, lost, {"state": "not_received"}, lost, {"state": "running"},
                   {"state": "completed", "result": result}]
        with patch("runtime.client.post", side_effect=replies) as calls, patch("runtime.client.time.sleep"):
            self.assertEqual(send_message("", "token", {"session": "a" * 32}, "append"), result)
        operations = [call.args[1].split("/")[-1] for call in calls.call_args_list]
        self.assertEqual(operations, ["message", "status", "status", "message", "status", "status"])
        self.assertEqual(len({call.args[2]["message_id"] for call in calls.call_args_list}), 1)

    def test_closed_session_or_failed_command_never_triggers_resubmission(self):
        for response, kind in [(Fault("session", "closed", 410), "unconfirmed"),
                               ({"state": "failed", "kind": "transport", "message": "manager lost", "status": 502},
                                "transport")]:
            with self.subTest(kind=kind), patch("runtime.client.post", side_effect=[
                TransportError("transport", "no response", 502), response
            ]) as calls, patch("runtime.client.time.sleep"):
                with self.assertRaises(Fault) as failed:
                    send_message("", "token", {"session": "b" * 32}, "append")
                self.assertEqual(failed.exception.kind, kind)
                self.assertEqual(calls.call_count, 2)
