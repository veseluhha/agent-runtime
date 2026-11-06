import unittest
from unittest.mock import patch
from .gateway import dispatch
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
