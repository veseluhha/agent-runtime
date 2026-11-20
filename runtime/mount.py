"""Expose a client directory to a Linux Docker host over a passive SSHFS connection."""
import os
import shlex
import socket
import subprocess
import sys
import tempfile
import time
import uuid
from contextlib import ExitStack, contextmanager
from pathlib import Path

from .edits import WorkingCopy, snapshot

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
def _mount(folder, host, config, access):
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
              "--exclude", ".git", "--exclude", ".git/**",
              "--exclude", ".agent-edits", "--exclude", ".agent-edits/**"]
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


@contextmanager
def workspace_mount(folder, host, config, access):
    if access not in ("ro", "rw"):
        raise ValueError("expected ro or rw")
    root = folder.resolve(strict=True)
    with ExitStack() as stack:
        edits = None
        exported = root
        if access == "rw":
            archive = root / ".agent-edits"
            archive.mkdir(mode=0o700, exist_ok=True)
            if archive.is_symlink():
                raise ValueError("invalid .agent-edits directory")
            exported = Path(stack.enter_context(tempfile.TemporaryDirectory(prefix="work-", dir=archive)))
            edits = WorkingCopy(root, exported)
            with edits.locked():
                check_conflicts(edits.recover())
            edits.refresh()
        try:
            with _mount(exported, host, config, access) as mount_id:
                yield mount_id, edits
        except EditConflict:
            raise
        except BaseException:
            if edits:
                try:
                    files, directories = snapshot(exported)
                    changed = ({p: v.contents for p, v in files.items()} !=
                               {p: v.contents for p, v in edits.base.items()} or directories.keys() != edits.directories.keys())
                except (OSError, ValueError):
                    changed = True
                if changed:
                    draft = exported.with_name("unfinished-" + exported.name)
                    exported.rename(draft)
                    print(f"незавершенная рабочая копия сохранена в {draft}", file=sys.stderr)
            raise


class EditConflict(RuntimeError):
    pass


def check_conflicts(conflicts):
    if conflicts:
        paths = "\n".join(f"{name} -- {proposal}" for name, proposal in conflicts)
        raise EditConflict("конфликт правок, версии агента сохранены на клиенте\n" + paths)


@contextmanager
def export_directory(folder, host, config, access):
    """Single-batch interface for callers which only need a mount ID."""
    with workspace_mount(folder, host, config, access) as (mount_id, edits):
        yield mount_id
        if edits:
            check_conflicts(edits.publish())
