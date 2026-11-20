import unittest
from unittest.mock import patch
from .gateway import dispatch
from .workspaces import workspace_request
from .http_api import Fault, post


class GatewayTest(unittest.TestCase):
    def test_authenticated_run_uses_model_recording(self):
        row = {"task_id": "B1.1", "pair": "v0+gpt-6-astra", "pass": False}
        with patch.dict("os.environ", {"CLIENT_TOKEN": "test-token"}):
            with patch("runtime.gateway.run", return_value=[row]) as run:
                result = dispatch("/v0/smoke", {"task_id": "B1.1"},
                                  {"Authorization": "Bearer test-token"})
        self.assertEqual(result, row)
        self.assertEqual(run.call_args.args[0], "record")
        self.assertEqual(run.call_args.kwargs["task_ids"], ["B1.1"])

    def test_bad_token_cannot_start_run(self):
        with self.assertRaises(Fault) as error:
            post("http://127.0.0.1:8080", "/v0/smoke", {"task_id": "B1.1"}, token="wrong-token")
        self.assertEqual(error.exception.status, 401)

    def test_workspace_messages_share_history_and_close_is_repeatable(self):
        session = "1" * 32
        requests = []

        def service(base, path, body, **kwargs):
            if path == "/v0/provision":
                self.assertEqual(body, {"mount_id": "a" * 32, "access": "ro"})
                return {"session": session}
            if path == "/v0/model":
                return {"model": "test"}
            if path == "/v0/completion":
                messages = body["request"]["messages"]
                requests.append([m["content"] for m in messages if m["role"] != "system"])
                return {"choices": [{"finish_reason": "stop", "message": {"role": "assistant", "content": "ok"}}]}
            self.assertIn(path, ("/v0/workdir", "/v0/release"))
            return {}

        with patch("runtime.workspaces.post", side_effect=service) as calls:
            opened = workspace_request("/v0/workspaces/open", {"mount_id": "a" * 32, "access": "ro"})
            try:
                self.assertEqual(opened["session"], session)
                for prompt in ("first", "second"):
                    reply = workspace_request("/v0/workspaces/message", {"session": session, "message": prompt})
                    self.assertEqual(reply["answer"], "ok")
                self.assertEqual(requests, [["first"], ["first", "ok", "second"]])
            finally:
                workspace_request("/v0/workspaces/close", {"session": session})
            workspace_request("/v0/workspaces/close", {"session": session})
            self.assertEqual(sum(call.args[1] == "/v0/release" for call in calls.call_args_list), 2)
            with self.assertRaises(Fault) as closed:
                workspace_request("/v0/workspaces/message", {"session": session, "message": "third"})
            self.assertEqual(closed.exception.kind, "session")

    def test_disconnect_before_final_answer_ends_workspace(self):
        session = "2" * 32
        with patch("runtime.workspaces.post", side_effect=[
            {"model": "test"}, {"session": session}, {"mounted": True},
            {"choices": [{"finish_reason": "stop", "message": {"role": "assistant", "content": "done"}}]},
            Fault("mount_lost", "client directory is unavailable", 503), {"released": True}
        ]) as services:
            workspace_request("/v0/workspaces/open", {"mount_id": "b" * 32, "access": "rw"})
            with self.assertRaises(Fault) as error:
                workspace_request("/v0/workspaces/message", {"session": session, "message": "read the file"})
            self.assertEqual(error.exception.kind, "mount_lost")
            self.assertEqual(services.call_args.args[1], "/v0/release")

    def test_key_replays_one_result_and_rejects_a_different_prompt(self):
        session, key = "3" * 32, "a" * 32
        body = {"session": session, "message_id": key}
        message = {**body, "message": "append once"}
        reply = {"choices": [{"finish_reason": "stop", "message": {"role": "assistant", "content": "ok"}}]}

        def service(base, path, request, **kwargs):
            if path.endswith("/provision"):
                return {"session": session}
            if path.endswith("/model"):
                return {"model": "test"}
            if path.endswith("/completion"):
                self.assertEqual(workspace_request("/v0/workspaces/status", body), {"state": "running"})
                for payload, kind in [(message, "in_progress"), ({**message, "message_id": "b" * 32}, "busy")]:
                    with self.assertRaises(Fault) as error:
                        workspace_request("/v0/workspaces/message", payload)
                    self.assertEqual(error.exception.kind, kind)
                return reply
            return {}

        with patch("runtime.workspaces.post", side_effect=service) as calls:
            workspace_request("/v0/workspaces/open", {"mount_id": "e" * 32, "access": "rw"})
            try:
                self.assertEqual(workspace_request("/v0/workspaces/status", body), {"state": "not_received"})
                result = workspace_request("/v0/workspaces/message", message)
                self.assertEqual(workspace_request("/v0/workspaces/message", message), result)
                self.assertEqual(workspace_request("/v0/workspaces/status", body),
                                 {"state": "completed", "result": result})
                with self.assertRaises(Fault) as mismatch:
                    workspace_request("/v0/workspaces/message", {**message, "message": "another prompt"})
                self.assertEqual(mismatch.exception.kind, "message_id")
                self.assertEqual(sum(c.args[1] == "/v0/completion" for c in calls.call_args_list), 1)
            finally:
                workspace_request("/v0/workspaces/close", {"session": session})
            with self.assertRaises(Fault) as expired:
                workspace_request("/v0/workspaces/status", body)
            self.assertEqual(expired.exception.status, 410)

    def test_failed_key_retains_the_fault_after_releasing_the_sandbox(self):
        session = "4" * 32
        key = {"session": session, "message_id": "c" * 32}
        message = {**key, "message": "read file"}
        with patch("runtime.workspaces.post", side_effect=[
            {"model": "test"}, {"session": session}, Fault("mount_lost", "gone", 503), {}, {}
        ]) as calls:
            workspace_request("/v0/workspaces/open", {"mount_id": "d" * 32, "access": "ro"})
            try:
                for _ in range(2):
                    with self.assertRaises(Fault) as fault:
                        workspace_request("/v0/workspaces/message", message)
                    self.assertEqual(fault.exception.kind, "mount_lost")
                self.assertEqual(workspace_request("/v0/workspaces/status", key),
                                 {"state": "failed", "kind": "mount_lost", "message": "gone", "status": 503})
                self.assertEqual(calls.call_count, 4)
            finally:
                workspace_request("/v0/workspaces/close", {"session": session})
