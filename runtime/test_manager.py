import unittest

from .http_api import Fault, post

URL = "http://127.0.0.1:8080"


class SandboxTest(unittest.TestCase):
    def setUp(self):
        self.session = post(URL, "/v0/provision", {})["session"]
        self.addCleanup(self.call, "release")

    def call(self, op, **body):
        return post(URL, "/v0/" + op, {"session": self.session, **body})

    def test_isolation(self):
        result = self.call("exec", argv=["sh", "-ec",
            'test "$(id -u)" = 65534; test -z "${CLIENT_TOKEN+x}"; test ! -e /var/run/docker.sock; '
            'test ! -w /etc; test ! -e /sys/class/net/eth0; touch /work/file'])
        self.assertEqual(result["exit_code"], 0, result)
        self.assertEqual(self.call("exec", argv=["sh", "-c", "exit 7"])["exit_code"], 7)

    def test_timeout_removes_container_and_children(self):
        with self.assertRaises(Fault) as error:
            self.call("exec", argv=["sh", "-c", "sleep 45 & wait"], timeout_s=1)
        self.assertEqual(error.exception.kind, "timeout")
        with self.assertRaises(Fault) as missing:
            self.call("exec", argv=["true"])
        self.assertEqual(missing.exception.kind, "sandbox")

    def test_release_is_repeatable(self):
        self.assertTrue(self.call("cancel")["released"])
        self.assertTrue(self.call("release")["released"])

    def test_sessions_have_separate_workdirs(self):
        self.assertEqual(self.call("exec", argv=["touch", "marker"])["exit_code"], 0)
        other = post(URL, "/v0/provision", {})["session"]
        self.addCleanup(post, URL, "/v0/release", {"session": other})
        result = post(URL, "/v0/exec", {"session": other, "argv": ["test", "!", "-e", "marker"]})
        self.assertEqual(result["exit_code"], 0)

    def test_output_limit(self):
        with self.assertRaises(Fault) as error:
            self.call("exec", argv=["yes"])
        self.assertEqual(error.exception.kind, "output_limit")
