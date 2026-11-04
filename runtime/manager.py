import os
import re
import selectors
import subprocess
import time
import uuid

from .http_api import Fault, serve

IMAGE = "python:3.12.10-alpine3.21"


def docker(*args, check=True):
    try:
        result = subprocess.run(["docker", *args], capture_output=True, timeout=15)
    except subprocess.TimeoutExpired as exc:
        raise Fault("timeout", "Docker request timed out", 504) from exc
    if check and result.returncode:
        raise Fault("sandbox", "Docker operation failed", 502)
    return result


def execute(name, argv, timeout):
    # Bound the stream before buffering it in the manager
    deadline = time.monotonic() + timeout
    output = bytearray()
    with subprocess.Popen(["docker", "exec", name, *argv], stdout=subprocess.PIPE,
                          stderr=subprocess.STDOUT) as proc, selectors.DefaultSelector() as poll:
        poll.register(proc.stdout, selectors.EVENT_READ)
        try:
            while True:
                if not poll.select(max(0, deadline - time.monotonic())) or time.monotonic() >= deadline:
                    raise Fault("timeout", "command timed out", 504)
                chunk = os.read(proc.stdout.fileno(), 8192)
                if not chunk:
                    break
                output.extend(chunk)
                if len(output) > 65536:
                    raise Fault("output_limit", "command exceeded 64 KiB", 413)
            code = proc.wait(timeout=max(0.01, deadline - time.monotonic()))
        except subprocess.TimeoutExpired as exc:
            proc.kill()
            docker("rm", "-f", name, check=False)
            raise Fault("timeout", "command timed out", 504) from exc
        except Fault:
            proc.kill()
            docker("rm", "-f", name, check=False)
            raise
    if code in (125, 126, 127, 1) and docker("inspect", name, check=False).returncode:
        raise Fault("sandbox", "sandbox is unavailable", 410)
    return {"exit_code": code, "output": output.decode(errors="replace")}


def sandbox_args(name, mount=None):
    work_mount = (["--mount", f"type=bind,src={mount},dst=/work"] if mount else
                  ["--tmpfs", "/work:rw,nosuid,nodev,size=16m,uid=65534,gid=65534,mode=0700"])
    return ["run", "-d", "--rm", "--pull=never", "--name", name,
           "--network=none", "--cpus=1", "--memory=128m", "--memory-swap=128m",
           "--pids-limit=32", "--read-only", "--cap-drop=ALL",
           "--security-opt=no-new-privileges", "--user=65534:65534",
           *work_mount,
           "-w", "/work", IMAGE, "sleep", "180"]


def dispatch(path, body, headers):
    prefix = os.environ["INSTANCE"] + "-work-"
    if path == "/v0/provision":
        session = uuid.uuid4().hex
        docker(*sandbox_args(prefix + session))
        return {"session": session}
    session = body.get("session")
    if not isinstance(session, str) or not re.fullmatch("[0-9a-f]{32}", session):
        raise Fault("invalid", "invalid session")
    name = prefix + session
    if path in ("/v0/release", "/v0/cancel"):
        exists = docker("container", "ls", "-aq", "--filter", "name=^/" + name + "$")
        if exists.stdout.strip():
            docker("rm", "-f", name)
        return {"released": True}
    if path == "/v0/exec":
        argv = body["argv"]
        timeout = body.get("timeout_s", 10)
        if not isinstance(argv, list) or not argv or not all(isinstance(s, str) and "\0" not in s for s in argv):
            raise Fault("invalid", "expected argv")
        if type(timeout) is not int or not 1 <= timeout <= 30:
            raise Fault("invalid", "timeout_s must be 1..30")
        return execute(name, argv, timeout)
    raise Fault("unknown", "unknown endpoint", 404)


if __name__ == "__main__":
    serve(dispatch, lambda: docker("info", "--format", "{{.ServerVersion}}"))
