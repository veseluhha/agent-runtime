"""Exercise the workspace API from a client connected to a Linux Docker host."""
import argparse
import json
import os
import platform
import secrets
import subprocess
import tempfile
from contextlib import ExitStack, contextmanager
from datetime import datetime, timezone
from pathlib import Path

from runtime.http_api import Fault, post
from runtime.mount import export_directory
from .probe import Server, line, run, stop


INTERRUPTED_WRITE = """
import sys
direct = open('direct.txt', 'wb', buffering=0)
staged = open('.staged.tmp', 'wb', buffering=0)
for stream in (direct, staged):
    stream.write(b'x' * 4096)
print('first block written', flush=True)
sys.stdin.readline()
for stream in (direct, staged):
    try:
        stream.write(b'y' * 4096)
        stream.close()
    except OSError:
        continue
    raise SystemExit('write unexpectedly completed after disconnect')
"""


@contextmanager
def mounted(work, server, host, config, access, request):
    with ExitStack() as cleanup:
        connection = cleanup.enter_context(ExitStack())
        mount_id = connection.enter_context(export_directory(work, host, config, access))
        opened = request("open", mount_id=mount_id, access=access)
        session = opened["session"]
        cleanup.callback(request, "close", session=session)
        name = server.run("docker", "ps", "--filter", "name=-work-" + session + "$", "--format", "{{.Names}}")
        assert name and server.run("docker", "exec", name, "id", "-u") == "65534"
        yield opened, name, connection.close
    server.run("test", "!", "-e", "/tmp/agent-runtime-" + mount_id)
    assert not server.run("docker", "ps", "-aq", "--filter", "name=-work-" + session + "$")


def interrupted_edit(work, server, host, config, request):
    for filename in ("direct.txt", "staged.txt", "shared.txt"):
        (work / filename).write_bytes(b"original")
    with mounted(work, server, host, config, "rw", request) as (_, name, disconnect):
        server.run("docker", "exec", name, "sh", "-ec", "cat shared.txt > .next && printf '+agent' >> .next")
        assert (work / ".next").read_bytes() == b"original+agent", "append lost bytes on client"
        (work / "shared.txt").write_bytes(b"client edit")
        server.run("docker", "exec", name, "python", "-c", "import os; os.replace('.next', 'shared.txt')")
        replaced = (work / "shared.txt").read_text()
        assert replaced == "original+agent", repr(replaced)
        writer = subprocess.Popen(server.cmd("docker", "exec", "-i", name, "python", "-u", "-c", INTERRUPTED_WRITE),
                                  stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            assert line(writer, 8) == "first block written"
            disconnect()
            _, stderr = writer.communicate(b"continue\n", timeout=10)
            assert writer.returncode == 0, stderr.decode(errors="replace")
        finally:
            stop(writer)
    assert (work / "staged.txt").read_bytes() == b"original"
    direct = (work / "direct.txt").read_bytes()
    temporary = work / ".staged.tmp"
    staged = temporary.read_bytes() if temporary.exists() else None
    assert direct in (b"original", b"x" * 4096), repr(direct[:20])
    assert staged in (None, b"x" * 4096)
    return {"append": "original+agent", "direct_bytes": len(direct), "direct_unchanged": direct == b"original",
            "staging_bytes": len(staged) if staged is not None else None, "staged_target": "original",
            "client_edit_before_rename": "client edit", "target_after_rename": replaced}


def verify(url, host, config):
    server = Server(host, config)
    token = os.environ["CLIENT_TOKEN"]

    def request(operation, **body):
        return post(url, "/v0/workspaces/" + operation, body, token=token, timeout=95)

    project = Path(__file__).resolve().parents[2]
    fixture = project / "baf/B4-edit/B4.1-install-version/fixture/README.md"
    original = fixture.read_bytes()
    expected = original.replace(b"pip install orders-cli==1.2", b"pip install orders-cli==1.3", 1)
    result = {"client": platform.system() + " " + platform.release(), "server": server.run("uname", "-srmo"),
              "rclone": run("rclone", "version").splitlines()[0],
              "sshfs": server.run("sshfs", "--version").splitlines()[0],
              "docker": server.run("docker", "version", "--format", "{{.Server.Version}}")}
    with tempfile.TemporaryDirectory(prefix="workspace-client-") as tmp:
        work = Path(tmp)
        readme = work / "README.md"
        readme.write_bytes(original)
        (work / ".git").mkdir()
        (work / ".git/marker").write_text("metadata")
        result["interrupted_write"] = interrupted_edit(work, server, host, config, request)
        with mounted(work, server, host, config, "rw", request) as (opened, _, _):
            result["model"] = opened["model"]
            session = opened["session"]
            reply = request("message", session=session, message=(
                "Измени версию orders-cli на 1.3 только в разделе Install файла README.md. "
                "Сохрани остальные байты. Используй временный файл и переименование."))
            assert readme.read_bytes() == expected, "unexpected README bytes on client"
            nonce = secrets.token_hex(8)
            readme.write_bytes(expected + b"\nReceipt: " + nonce.encode() + b"\n")
            second = request("message", session=session, message=(
                "Прочитай README.md еще раз с диска и верни значение Receipt из последней строки"))
            assert nonce in (second["answer"] or "") and second["events"], "client edit not observed"
            result["edit"] = {"before": original.decode(), "after": expected.decode(),
                              "events": reply["events"], "read_events": second["events"],
                              "client_value": nonce, "answer": second["answer"]}
        with mounted(work, server, host, config, "ro", request) as (opened, name, disconnect):
            server.run("docker", "exec", name, "sh", "-ec", "test ! -e .git/marker && ! touch forbidden")
            assert not (work / "forbidden").exists()
            result["read_only"] = True
            disconnect()
            try:
                request("message", session=opened["session"], message="Прочитай README.md")
            except Fault as exc:
                if exc.kind != "mount_lost":
                    raise
                result["disconnect"] = exc.kind
            else:
                raise AssertionError("disconnected workspace accepted a message")
    return {**result, "cleaned": True}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("host")
    parser.add_argument("--url", required=True)
    parser.add_argument("--config")
    args = parser.parse_args()
    if not __debug__:
        parser.error("run without -O")
    result = verify(args.url, args.host, args.config)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S-%fZ")
    path = Path("results/sshfs") / ("workspace-" + stamp + ".json")
    path.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    print(path)


if __name__ == "__main__":
    main()
