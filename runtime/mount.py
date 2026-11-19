"""Expose a client directory to a Linux Docker host over a passive SSHFS connection."""
import os
import shlex
import socket
import subprocess
import tempfile
import time
import uuid
from contextlib import ExitStack, contextmanager

SSHFS_OPTIONS = ("passive,allow_other,uid=65534,gid=65534,direct_io,dir_cache=no,"
                 "attr_timeout=0,entry_timeout=0,negative_timeout=0,nosuid,nodev")

# The remote shell owns its private directory until sshfs sees EOF
MOUNT = """
set -eu
umask 077
mkdir "$1"
trap 'rmdir "$1/work" "$1"' EXIT
mkdir "$1/work"
sshfs -f :/ "$1/work" -o "$2,auto_unmount"
"""


def stop_process(proc):
    if proc.poll() is None:
        proc.terminate()
    try:
        proc.wait(timeout=3)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()


@contextmanager
def export_directory(folder, host, config, access):
    folder = folder.resolve(strict=True)
    if not folder.is_dir():
        raise ValueError("workdir must be a directory")
    mount_id = uuid.uuid4().hex
    directory = "/tmp/agent-runtime-" + mount_id
    ssh = ["ssh", "-T", "-S", "none", "-o", "BatchMode=yes", "-o", "ForwardAgent=no",
           "-o", "ConnectTimeout=5", "-o", "ClearAllForwardings=yes"]
    if config:
        ssh.extend(["-F", str(config)])
    ssh.extend(["--", host])
    command = shlex.join(["sh", "-c", MOUNT, "mount-workdir", directory, SSHFS_OPTIONS + ",sshfs_sync," + access])
    rclone = ["rclone", "--config", "/dev/null", "serve", "sftp", str(folder), "--stdio",
              "--transfers", "1", "--dir-cache-time", "0s",
              "--exclude", ".git", "--exclude", ".git/**"]
    if access == "ro":
        rclone.append("--read-only")
    with ExitStack() as stack:
        if access == "rw":
            cache = stack.enter_context(tempfile.TemporaryDirectory(prefix="workdir-cache-"))
            rclone.extend(["--cache-dir", cache, "--vfs-cache-mode", "writes", "--vfs-write-back", "0s"])
        left, right = socket.socketpair()
        with left, right:
            server = subprocess.Popen(rclone, stdin=left, stdout=left,
                                      env={k: v for k, v in os.environ.items() if not k.startswith("RCLONE_")})
            stack.callback(stop_process, server)
            tunnel = subprocess.Popen([*ssh, command], stdin=right, stdout=right)
            stack.callback(stop_process, tunnel)
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline and tunnel.poll() is None and server.poll() is None:
            check = subprocess.run([*ssh, shlex.join(["findmnt", "-rn", "-M", directory + "/work", "-o", "FSTYPE"])],
                                   capture_output=True, text=True, timeout=6)
            if check.stdout.strip() == "fuse.sshfs" and check.returncode == 0:
                break
            time.sleep(0.1)
        else:
            raise RuntimeError("SSHFS did not mount the client directory")
        try:
            yield mount_id
        finally:
            stop_process(server)
            try:
                tunnel.wait(timeout=5)
            except subprocess.TimeoutExpired:
                stop_process(tunnel)
