"""Cut one client HTTP request while the Debian workspace and SSHFS stay connected."""
import json
import socket
import subprocess
import sys
import threading
import time
from http.client import HTTPConnection
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

APPEND = """set -eu
echo run >> executions.txt
echo agent >> ticket.txt
while test -e pause; do sleep 0.1; done
"""


def run_case(work, server, host, config, url, cut):
    target = work / "ticket.txt"
    target.write_text("base\n")
    (work / "executions.txt").write_bytes(b"")
    (work / "append.sh").write_text(APPEND)
    if cut == "during":
        (work / "pause").touch()
    origin = urlsplit(url)
    if origin.scheme != "http":
        raise ValueError("use a local HTTP tunnel to the gateway")
    trace, keys = [], set()
    errors = []
    opened = {}
    dropped = threading.Event()

    class Relay(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            try:
                self.forward()
            except Exception as exc:
                errors.append(exc)
                raise

        def forward(self):
            data = self.rfile.read(int(self.headers["Content-Length"]))
            body = json.loads(data)
            action = self.path.rsplit("/", 1)[-1]
            if action in ("message", "status"):
                keys.add((body["session"], body["message_id"]))
            fault = action == "message" and not dropped.is_set()
            if fault:
                dropped.set()
            if fault and cut == "before":
                trace.append("discarded before forwarding")
                self.connection.shutdown(socket.SHUT_RDWR)
                return
            conn = HTTPConnection(origin.hostname, origin.port, timeout=90)
            try:
                conn.request("POST", self.path, data, {"Content-Type": "application/json",
                                                      "Authorization": self.headers["Authorization"]})
                if fault and cut == "during":
                    deadline = time.monotonic() + 70
                    while not any(p.read_bytes() for p in work.glob(".agent-edits/work-*/executions.txt")):
                        if time.monotonic() >= deadline:
                            raise TimeoutError("shell did not reach the barrier")
                        time.sleep(0.04)
                    trace.append("shell appended, waiting at barrier")
                    self.connection.shutdown(socket.SHUT_RDWR)
                    return
                response = conn.getresponse()
                payload = response.read()
                value = json.loads(payload)
                if action == "open":
                    opened.update(value, mount_id=body["mount_id"])
                if action == "status":
                    state = value.get("state", value.get("error"))
                    trace.append(state)
                    if cut == "during" and state == "running":
                        for barrier in work.glob(".agent-edits/work-*/pause"):
                            barrier.unlink()
                if fault and cut in ("after", "edit"):
                    assert response.status == 200 and value["events"], value
                    trace.append("gateway returned the completed command")
                    if cut == "edit":
                        target.write_text("Mac\n")
                        trace.append("Mac editor saved")
                    self.send_response(200)
                    self.send_header("Content-Length", str(len(payload)))
                    self.end_headers()
                    self.wfile.write(payload[:5])
                    self.connection.shutdown(socket.SHUT_RDWR)
                    return
                self.send_response(response.status)
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
            finally:
                conn.close()

    with ThreadingHTTPServer(("127.0.0.1", 0), Relay) as relay:
        thread = threading.Thread(target=relay.serve_forever)
        thread.start()
        try:
            cmd = [sys.executable, "-m", "runtime.client", "--url", f"http://127.0.0.1:{relay.server_port}",
                   "--ssh-host", host, "--workdir", str(work), "--write", "--message",
                   "Run sh append.sh exactly once in /work and report completion."]
            if config:
                cmd.extend(["--ssh-config", str(config)])
            cli = subprocess.run(cmd, capture_output=True, text=True, timeout=155)
        finally:
            relay.shutdown()
            thread.join()
    if errors:
        raise errors[0]
    assert len(keys) == 1 and "восстанавливаю ответ" in cli.stderr, (trace, cli.stderr)
    expected = {"before": "not_received", "during": "running", "after": "completed", "edit": "completed"}
    assert expected[cut] in trace, (trace, cli.stderr)
    assert (work / "executions.txt").read_text() == "run\n", (trace, cli.stderr)
    local = target.read_text()
    proposal = None
    if cut == "edit":
        assert cli.returncode == 1 and local == "Mac\n" and "конфликт правок" in cli.stderr, cli.stderr
        path, = [Path(line.split(" -- ", 1)[1]) for line in cli.stderr.splitlines() if line.startswith("ticket.txt -- ")]
        proposal = path.read_text()
        assert proposal == "base\nagent\n"
    else:
        assert cli.returncode == 0 and local == "base\nagent\n", (trace, cli.stderr)
    assert not server.run("docker", "ps", "-aq", "--filter", "name=-work-" + opened["session"] + "$")
    server.run("test", "!", "-e", "/tmp/agent-runtime-" + opened["mount_id"])
    return {"cut": cut, "trace": trace, "model": opened["model"], "executions": 1, "cli_exit": cli.returncode,
            "ticket": local, "conflict_copy": proposal, "cleaned": True}
