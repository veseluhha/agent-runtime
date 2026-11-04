"""Probe a client directory mounted through SSHFS on a Linux Docker host."""
import argparse
import json
import os
import platform
import select
import shlex
import signal
import socket
import subprocess
import tempfile
import time
import uuid
from contextlib import ExitStack
from datetime import datetime, timezone
from pathlib import Path

from runtime.manager import IMAGE, sandbox_args

OPTIONS = ("passive,allow_other,uid=65534,gid=65534,direct_io,dir_cache=no,"
           "attr_timeout=0,entry_timeout=0,negative_timeout=0,nosuid,nodev")
READER = """
import json, os, sys
fd = os.open('/work/probe.bin', os.O_RDONLY)
assert len(os.read(fd, 4096)) == 4096
assert len(os.pread(fd, 4096, 65536)) == 4096
writer = os.open('/work/unfinished.bin', os.O_WRONLY | os.O_TRUNC)
assert os.write(writer, b'A' * 4096) == 4096
print('ready', flush=True)
sys.stdin.readline()
print('reading', flush=True)
try:
    data = os.pread(fd, 4096, 65536)
    read = {'bytes': len(data)}
except OSError as exc:
    read = {'errno': exc.errno, 'error': exc.strerror}
print(json.dumps(read), flush=True)
out = {}
try:
    out['second_write_bytes'] = os.write(writer, b'B' * 4096)
except OSError as exc:
    out['second_write_errno'] = exc.errno
try:
    os.close(writer)
    out['close_errno'] = 0
except OSError as exc:
    out['close_errno'] = exc.errno
print(json.dumps(out), flush=True)
"""


WRITE_AFTER_CUT = """
import json, os
try:
    fd = os.open('/work/after-cut', os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    os.write(fd, b'test')
    os.close(fd)
    print('{}')
except OSError as exc:
    print(json.dumps({'errno': exc.errno, 'error': exc.strerror}))
"""


def run(*args, check=True, timeout=15):
    p = subprocess.run(args, text=True, capture_output=True, timeout=timeout)
    if check and p.returncode:
        raise RuntimeError(f"{args[0]} {args[1]} failed: {(p.stderr or p.stdout).strip()}")
    return p.stdout.strip()


def stop(proc):
    if proc.poll() is None:
        proc.kill()
    proc.wait(timeout=5)


def line(proc, timeout):
    if not select.select([proc.stdout], [], [], timeout)[0]:
        raise TimeoutError("sandbox reader did not respond")
    text = proc.stdout.readline()
    if not text:
        raise RuntimeError("sandbox reader exited: " + proc.stderr.read().decode())
    return text.decode().strip()


class Server:
    def __init__(self, host, config):
        self.ssh = ["ssh", "-T", "-S", "none", "-o", "BatchMode=yes", "-o", "ForwardAgent=no",
                    "-o", "ClearAllForwardings=yes", "-o", "ConnectTimeout=5"]
        if config:
            self.ssh += ["-F", config]
        self.ssh += ["--", host]

    def cmd(self, *args):
        return [*self.ssh, shlex.join(args)]

    def run(self, *args):
        return run(*self.cmd(*args))


def unfinished_state(path):
    if not path.exists():
        return "missing"
    data = path.read_bytes()
    return {b"original": "original", b"": "empty", b"A" * 4096: "first_block",
            b"A" * 4096 + b"B" * 4096: "both_blocks"}.get(data, f"other ({len(data)} bytes)")


def trial(kind, work, server):
    sandbox = "sshfs-lab-" + uuid.uuid4().hex[:12]

    def agent(*cmd):
        return server.run("docker", "exec", sandbox, *cmd)

    parent = server.run("mktemp", "-d", "/tmp/sshfs-lab.XXXXXXXX")
    mount = parent + "/export"
    with ExitStack() as stack:
        stack.callback(server.run, "rmdir", parent)
        server.run("mkdir", "-m", "700", mount)
        stack.callback(server.run, "rmdir", mount)
        stack.callback(server.run, "sh", "-ec",
                       'if findmnt -rn -M "$1" >/dev/null; then fusermount3 -uz "$1"; fi', "cleanup", mount)
        ssh_log = stack.enter_context(tempfile.TemporaryFile(mode="w+"))
        sftp_log = stack.enter_context(tempfile.TemporaryFile(mode="w+"))
        a, b = socket.socketpair()
        with a, b:
            sftp = subprocess.Popen(["rclone", "--config", "/dev/null", "serve", "sftp", str(work), "--stdio",
                                     "--transfers", "1", "--dir-cache-time", "0s", "--vfs-cache-mode", "off"],
                                    stdin=a, stdout=a, stderr=sftp_log,
                                    env={k: v for k, v in os.environ.items() if not k.startswith("RCLONE_")})
            stack.callback(stop, sftp)
            ssh = subprocess.Popen(server.cmd("sshfs", "-f", ":/", mount, "-o", OPTIONS),
                                   stdin=b, stdout=b, stderr=ssh_log)
            stack.callback(stop, ssh)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and ssh.poll() is None:
            if run(*server.cmd("findmnt", "-rn", "-M", mount, "-o", "FSTYPE"), check=False) == "fuse.sshfs":
                break
            time.sleep(.1)
        else:
            ssh_log.seek(0)
            sftp_log.seek(0)
            raise RuntimeError("SSHFS did not mount: " + ssh_log.read() + sftp_log.read())

        server.run("docker", *sandbox_args(sandbox, mount))
        stack.callback(server.run, "docker", "rm", "-f", sandbox)
        fs = next(s.split(" - ")[1].split()[0] for s in agent("cat", "/proc/self/mountinfo").splitlines()
                  if s.split()[4] == "/work")
        assert fs == "fuse.sshfs", fs
        assert agent("id", "-u") == "65534"
        assert agent("ls", "/sys/class/net") == "lo"
        filename = work / "shared.txt"
        committed = work / "committed.txt"
        if kind == "unresponsive":
            assert agent("cat", "shared.txt") == "client-3"
            assert agent("cat", "committed.txt") == "saved"
            committed.unlink()
        filename.write_text("client-1")
        assert agent("cat", filename.name) == "client-1"
        agent("sh", "-ec", "printf agent-2 > shared.txt; printf saved > committed.txt")
        assert filename.read_text() == "agent-2"
        filename.write_text("client-3")
        assert agent("cat", filename.name) == "client-3"

        unfinished = work / "unfinished.bin"
        unfinished.write_bytes(b"original")
        reader = subprocess.Popen(server.cmd("docker", "exec", "-i", sandbox, "python", "-u", "-c", READER),
                                  stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0)
        stack.callback(stop, reader)
        assert line(reader, 8) == "ready"
        ssh.send_signal(signal.SIGSTOP)
        reader.stdin.write(b"go\n")
        assert line(reader, 3) == "reading"
        started = time.monotonic()
        assert not select.select([reader.stdout], [], [], .3)[0], "read did not wait for SFTP"
        if kind == "disconnect":
            cut = time.monotonic()
            ssh.kill()
        failure = json.loads(line(reader, 25))
        finished = time.monotonic()
        assert failure.get("errno") or failure.get("bytes") == 0, failure
        write_outcome = json.loads(line(reader, 5))
        assert reader.wait(timeout=5) == 0

        normal_exec = subprocess.run(server.cmd("docker", "exec", sandbox, "pwd"),
                                     capture_output=True, text=True, timeout=8)
        write_failure = json.loads(server.run("docker", "exec", "-w", "/", sandbox, "python", "-c", WRITE_AFTER_CUT))
        assert write_failure.get("errno") and not (work / "after-cut").exists()
        assert filename.read_text() == "client-3"
        assert committed.read_text() == "saved"
        listed = mount in server.run("findmnt", "-rn", "-o", "TARGET").splitlines()
        agent_mounts = server.run("docker", "exec", "-w", "/", sandbox, "cat", "/proc/self/mountinfo")
        dead_bind = any(line.split()[4] == "/work" and " - fuse.sshfs " in line for line in agent_mounts.splitlines())
        running = server.run("docker", "inspect", "-f", "{{.State.Running}}", sandbox) == "true"
        result = {"fault": kind, "server_mount_listed_before_cleanup": listed,
                  "sandbox_running_before_cleanup": running, "agent_sshfs_listed_after_cut": dead_bind, "filesystem": fs, "agent_uid": 65534, "agent_interfaces": ["lo"],
                  "observed_edits": ["client-1", "agent-2", "client-3"],
                  "read_blocked_for_s": round(finished - started, 3),
                  "after_kill_s": round(finished - cut, 3) if kind == "disconnect" else None,
                  "read": failure, "healthy_pread_bytes": 4096, "new_file_after_cut": write_failure, "open_writer": write_outcome,
                  "exec_in_work": {"code": normal_exec.returncode, "output": (normal_exec.stdout + normal_exec.stderr).strip()},
                  "unfinished_before_cleanup": unfinished_state(unfinished),
                  "client_file_after_cut": filename.read_text(), "new_file_visible_on_client": False,
                  "client_has_acknowledged_write": True, "fresh_session_read_previous_edits": True if kind == "unresponsive" else None}
    server.run("test", "!", "-e", parent)
    assert mount not in server.run("findmnt", "-rn", "-o", "TARGET").splitlines()
    assert sandbox not in server.run("docker", "ps", "-a", "--format", "{{.Names}}").splitlines()
    return {**result, "unfinished_after_cleanup": unfinished_state(unfinished),
            "unfinished_size_after_cleanup": unfinished.stat().st_size if unfinished.exists() else None, "cleaned": True}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("host", help="SSH host for the Linux server")
    parser.add_argument("--config", help="optional ssh_config")
    parser.add_argument("--repeats", type=int, default=1, help="pairs of disconnect and unresponsive trials")
    args = parser.parse_args()
    if args.repeats < 1 or not __debug__:
        parser.error("use at least one repeat and run without -O")
    server = Server(args.host, args.config)
    info = {"client": platform.system() + " " + platform.release(), "server": server.run("uname", "-srmo"),
            "docker_server": server.run("docker", "version", "--format", "{{.Server.Version}}"),
            "sshfs": server.run("sshfs", "--version").splitlines(),
            "rclone": run("rclone", "version").splitlines()[0], "sshfs_options": OPTIONS}
    server.run("docker", "image", "inspect", "--format", "{{.Id}}", IMAGE)
    rows = []
    with tempfile.TemporaryDirectory(prefix="sshfs-client-") as tmp:
        work = Path(tmp)
        (work / "probe.bin").write_bytes(os.urandom(128 * 1024))
        for i in range(args.repeats):
            for kind in ("disconnect", "unresponsive"):
                rows.append({"repeat": i + 1, **trial(kind, work, server)})
                print(json.dumps(rows[-1]), flush=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S-%fZ")
    output = Path("results/sshfs") / (stamp + ".json")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps({**info, "trials": rows}, indent=2) + "\n")
    print(output)


if __name__ == "__main__":
    main()
