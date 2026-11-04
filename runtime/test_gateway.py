import unittest
from .http_api import Fault, post


class GatewayTest(unittest.TestCase):
    def test_bad_token_cannot_start_run(self):
        with self.assertRaises(Fault) as error:
            post("http://127.0.0.1:8080", "/v0/smoke", {"task_id": "B1.1"}, token="wrong-token")
        self.assertEqual(error.exception.status, 401)
