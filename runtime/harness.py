import base64
import json
import math
import subprocess
import sys
import tempfile
import time
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


def run_smoke(task):
    started = time.monotonic()
    deadline = started + task.timeout_s
    events = []

    def remaining():
        seconds = deadline - time.monotonic()
        if seconds <= 0:
            raise Fault("timeout", "task deadline exceeded", 504)
        return seconds

    session = post(MANAGER, "/v0/provision", {}, timeout=min(30, remaining()))["session"]

    def command(argv, phase):
        start = time.monotonic()
        budget = min(10, math.ceil(remaining()))
        result = post(MANAGER, "/v0/exec", {"session": session, "argv": argv, "timeout_s": budget},
                      timeout=min(30, remaining()))
        event = {"phase": phase, "tool": argv[0], "exit_code": result["exit_code"],
                 "seconds": round(time.monotonic() - start, 4)}
        if phase == "agent":
            event["argv"] = argv
        events.append(event)
        if phase != "agent" and result["exit_code"]:
            raise Fault("command", f"{argv[0]} exited with {result['exit_code']}", 422)
        return result

    try:
        fixture = task.directory / "fixture"
        for path in sorted(fixture.rglob("*")):
            relative = str(path.relative_to(fixture))
            if path.is_dir():
                command(["mkdir", "-p", relative], "setup")
            else:
                command(["python", "-I", "-c", WRITE_FILE, relative, path.read_bytes().hex()], "setup")
        messages = [{"role": "user", "task": prompt} for prompt in task.messages]
        for _ in range(8):
            action = post("http://llm-proxy:8080", "/v0/next",
                          {"model": "scripted-smoke", "task_id": task.id, "messages": messages},
                          timeout=min(30, remaining()))
            if action.get("done") is True:
                break
            argv = action["argv"]
            result = command(argv, "agent")
            messages.append({"role": "tool", "argv": argv, "output": result["output"],
                             "exit_code": result["exit_code"]})
        else:
            raise Fault("step_limit", "model exceeded the step budget", 422)
        snapshot = command(["python", "-I", "-S", "-c", SNAPSHOT], "collect")["output"]
        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp)
            restore(json.loads(snapshot), work)
            checker = subprocess.run([sys.executable, str(task.directory / "check.py"), tmp],
                                     capture_output=True, timeout=min(5, remaining()))
            if checker.returncode:
                raise Fault("checker", "checker process failed", 500)
            verdict = json.loads(checker.stdout)
            answer = work / task.answer_file
            preview = answer.read_bytes()[:1024].decode(errors="replace") if answer.is_file() else None
    finally:
        post(MANAGER, "/v0/release", {"session": session})
    return {"task_id": task.id, "repeat": 1, "pair": "v0+scripted-mock", **verdict,
            "answer_preview": preview, "outcome": "completed", "trace_id": session, "events": events,
            "seconds": round(time.monotonic() - started, 4), "tokens_in": 0, "tokens_out": 0}
