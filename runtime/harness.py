import base64
import json
import math
import os
import subprocess
import sys
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path

from .http_api import Fault, post

MANAGER = "http://sandbox-manager:8080"
SNAPSHOT = Path(__file__).with_name("snapshot.py").read_text()
WRITE_FILE = "import pathlib,sys; pathlib.Path(sys.argv[1]).write_bytes(bytes.fromhex(sys.argv[2]))"


def restore(entries, work):
    for name, content in entries.items():
        relative = Path(name)
        if not relative.parts or relative.is_absolute() or ".." in relative.parts:
            raise Fault("collect", "invalid sandbox path", 422)
        path = work / relative
        if content is None:
            path.mkdir(parents=True, exist_ok=True)
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(base64.b64decode(content, validate=True))


class Sandbox:
    def __init__(self, session, deadline):
        self.deadline = deadline
        self.session = session
        self.events = []

    def remaining(self):
        seconds = self.deadline - time.monotonic()
        if seconds <= 0:
            raise Fault("timeout", "task deadline exceeded", 504)
        return seconds

    def command(self, argv, phase):
        start = time.monotonic()
        budget = min(10, math.ceil(self.remaining()))
        result = post(MANAGER, "/v0/exec", {"session": self.session, "argv": argv, "timeout_s": budget},
                      timeout=min(30, self.remaining()))
        event = {"phase": phase, "tool": argv[0], "exit_code": result["exit_code"],
                 "seconds": round(time.monotonic() - start, 4)}
        if phase == "agent":
            event["argv"] = argv
        self.events.append(event)
        if phase != "agent" and result["exit_code"]:
            raise Fault("command", f"{argv[0]} exited with {result['exit_code']}", 422)
        return result

    def run(self, prompts, next_action):
        history = [{"role": "user", "task": prompt} for prompt in prompts]
        for _ in range(8):
            action = next_action(history, self.remaining())
            if action.get("done") is True:
                return action.get("text")
            for argv in action["commands"]:
                result = self.command(argv, "agent")
                history.append({"role": "tool", "argv": argv, **result})
        raise Fault("step_limit", "model exceeded the step budget", 422)


@contextmanager
def fresh_sandbox(timeout_s):
    deadline = time.monotonic() + timeout_s
    session = post(MANAGER, "/v0/provision", {}, timeout=min(30, timeout_s))["session"]
    try:
        yield Sandbox(session, deadline)
    finally:
        post(MANAGER, "/v0/release", {"session": session})


def run_task(task, next_action):
    started = time.monotonic()
    with fresh_sandbox(task.timeout_s) as sandbox:
        fixture = task.directory / "fixture"
        for path in sorted(fixture.rglob("*")):
            relative = str(path.relative_to(fixture))
            if path.is_dir():
                sandbox.command(["mkdir", "-p", relative], "setup")
            else:
                sandbox.command(["python", "-I", "-c", WRITE_FILE, relative, path.read_bytes().hex()], "setup")
        sandbox.run(task.messages, next_action)
        snapshot = sandbox.command(["python", "-I", "-S", "-c", SNAPSHOT], "collect")["output"]
        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp)
            restore(json.loads(snapshot), work)
            try:
                checker = subprocess.run([sys.executable, str(task.directory / "check.py"), tmp],
                                         env={**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1])},
                                         capture_output=True, timeout=min(task.checker_timeout_s, sandbox.remaining()))
            except subprocess.TimeoutExpired as exc:
                raise Fault("checker", "checker deadline exceeded", 504) from exc
            if checker.returncode:
                raise Fault("checker", "checker process failed", 500)
            verdict = json.loads(checker.stdout)
            answer = work / task.answer_file
            preview = answer.read_bytes()[:1024].decode(errors="replace") if answer.is_file() else None
    return {"task_id": task.id, "repeat": 1, **verdict,
            "answer_preview": preview, "outcome": "completed", "trace_id": sandbox.session, "events": sandbox.events,
            "seconds": round(time.monotonic() - started, 4)}
