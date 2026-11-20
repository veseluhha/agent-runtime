"""Exercise the workspace API from a client connected to a Linux Docker host."""
import argparse
import io
import json
import os
import platform
import secrets
import subprocess
import sys
import tempfile
from contextlib import ExitStack, contextmanager, redirect_stdout
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from runtime.http_api import Fault, post
from runtime import client, edits as publication
from runtime.mount import workspace_mount
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
        mount_id, edits = connection.enter_context(workspace_mount(work, host, config, access))
        opened = request("open", mount_id=mount_id, access=access)
        session = opened["session"]
        cleanup.callback(request, "close", session=session)
        name = server.run("docker", "ps", "--filter", "name=-work-" + session + "$", "--format", "{{.Names}}")
        assert name and server.run("docker", "exec", name, "id", "-u") == "65534"
        yield opened, name, connection.close, edits
    server.run("test", "!", "-e", "/tmp/agent-runtime-" + mount_id)
    assert not server.run("docker", "ps", "-aq", "--filter", "name=-work-" + session + "$")


def competing_edits(work, server, host, config, request):
    target = work / "shared.txt"
    results = []
    for moment in ("clean", "before_check", "before_exchange", "open_fd_before_exchange", "during_restore"):
        target.write_bytes(b"base")
        steps = []
        with mounted(work, server, host, config, "rw", request) as (_, name, _, edits):
            direct = moment == "before_check"
            command = ("printf '+agent' >> shared.txt" if direct else
                       "cat shared.txt > .next && printf '+agent' >> .next && mv .next shared.txt")
            server.run("docker", "exec", name, "sh", "-ec", command)
            assert target.read_bytes() == b"base"
            steps.append("Debian wrote staging with " + ("append" if direct else "rename") + "; Mac still contains base")
            with target.open("r+b") as editor_fd:
                def editor_save(value):
                    subprocess.run([sys.executable, "-c",
                                    "import os,sys; from pathlib import Path; p=Path(sys.argv[1]); "
                                    "t=p.with_suffix('.saving'); t.write_bytes(sys.argv[2].encode()); os.replace(t,p)",
                                    str(target), value], check=True)
                    steps.append("Mac editor saved " + value)

                if moment == "before_check":
                    editor_save("user")
                rename = publication.rename_at

                def synchronized_rename(source, parent, dest, *, exchange=False):
                    if exchange:
                        steps.append("base check passed")
                        if moment in ("before_exchange", "during_restore"):
                            editor_save("user")
                        elif moment == "open_fd_before_exchange":
                            editor_fd.seek(0)
                            editor_fd.write(b"user")
                            editor_fd.flush()
                            os.fsync(editor_fd.fileno())
                            steps.append("Mac saved user through an already open descriptor")
                        steps.append("atomic exchange")
                    elif moment == "during_restore" and source.name == "exchange":
                        editor_save("last")
                    return rename(source, parent, dest, exchange=exchange)

                with patch.object(publication, "rename_at", synchronized_rename):
                    conflicts = edits.publish()
            local = target.read_text()
            expected = "base+agent" if moment == "clean" else ("last" if moment == "during_restore" else "user")
            assert local == expected, (moment, local)
            assert len(conflicts) == int(moment != "clean")
            proposal = conflicts[0][1].read_text() if conflicts else None
            if conflicts:
                assert conflicts[0][0] == Path("shared.txt") and proposal == "base+agent"
            if moment == "during_restore":
                previous = conflicts[0][1].parents[1] / "previous"
                assert previous.read_text() == "user"
            if moment == "clean":
                target.write_text("Mac changed this file between messages")
                edits.refresh()
                assert server.run("docker", "exec", name, "cat", "shared.txt") == target.read_text()
                steps.append("next Debian turn read the new Mac contents")
            results.append({"moment": moment, "steps": steps, "original_path": local,
                            "agent_conflict_file": proposal, "conflicts": len(conflicts)})
    return results


def cli_conflict(work, url, host, config):
    target = work / "ticket.txt"
    target.write_text("base\n")
    args = ["client", "--url", url, "--ssh-host", host, "--workdir", str(work), "--write",
            "--message", "Прочитай ticket.txt и замени его содержимое ровно на AGENT и перевод строки"]
    if config:
        args.extend(["--ssh-config", config])
    rename = publication.rename_at

    def editor_at_exchange(source, parent, name, **flags):
        if name == target.name and flags.get("exchange"):
            subprocess.run([sys.executable, "-c",
                            "from pathlib import Path; import sys; Path(sys.argv[1]).write_bytes(b'MAC\\n')",
                            str(target)], check=True)
        return rename(source, parent, name, **flags)

    output = io.StringIO()
    try:
        with patch.object(sys, "argv", args), patch.object(publication, "rename_at", editor_at_exchange), redirect_stdout(output):
            client.main()
    except SystemExit as failure:
        message = str(failure)
        if "конфликт правок" not in message:
            raise RuntimeError(f"live CLI did not reach publication: {message}") from failure
        assert "ticket.txt" in message, message
    else:
        raise AssertionError("CLI accepted a stale edit")
    proposals = [p for p in (work / ".agent-edits").glob("edit-*/agent/ticket.txt") if str(p) in message]
    assert len(proposals) == 1 and proposals[0].read_bytes() == b"AGENT\n" and target.read_bytes() == b"MAC\n"
    lines = output.getvalue().splitlines()
    return {"model": lines[0].split(" | ", 1)[0], "commands": lines[1:],
            "status": "conflict", "path_reported": True,
            "original_path": target.read_text(), "agent_conflict_file": proposals[0].read_text()}


def interrupted_edit(work, server, host, config, request):
    for filename in ("direct.txt", "staged.txt"):
        (work / filename).write_bytes(b"original")
    with mounted(work, server, host, config, "rw", request) as (_, name, disconnect, _):
        writer = subprocess.Popen(server.cmd("docker", "exec", "-i", name, "python", "-u", "-c", INTERRUPTED_WRITE),
                                  stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            assert line(writer, 8) == "first block written"
            disconnect()
            _, stderr = writer.communicate(b"continue\n", timeout=10)
            assert writer.returncode == 0, stderr.decode(errors="replace")
        finally:
            stop(writer)
    assert (work / "direct.txt").read_bytes() == (work / "staged.txt").read_bytes() == b"original"
    assert not (work / ".staged.tmp").exists()
    return {"direct_unchanged": True, "staged_target": "original"}


def verify(url, host, config, scenario):
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
        if scenario in ("all", "conflicts"):
            result["competing_edits"] = competing_edits(work, server, host, config, request)
            result["interrupted_write"] = interrupted_edit(work, server, host, config, request)
        if scenario in ("all", "cli-conflict"):
            result["cli_conflict"] = cli_conflict(work, url, host, config)
        if scenario != "all":
            return {**result, "scenario": scenario, "cleaned": True}
        with mounted(work, server, host, config, "rw", request) as (opened, _, _, edits):
            result["model"] = opened["model"]
            session = opened["session"]
            reply = request("message", session=session, message=(
                "Измени версию orders-cli на 1.3 только в разделе Install файла README.md. "
                "Сохрани остальные байты. Используй временный файл и переименование."))
            assert edits.publish() == []
            assert readme.read_bytes() == expected, "unexpected README bytes on client"
            nonce = secrets.token_hex(8)
            readme.write_bytes(expected + b"\nReceipt: " + nonce.encode() + b"\n")
            edits.refresh()
            second = request("message", session=session, message=(
                "Прочитай README.md еще раз с диска и верни значение Receipt из последней строки"))
            assert nonce in (second["answer"] or "") and second["events"], "client edit not observed"
            result["edit"] = {"before": original.decode(), "after": expected.decode(),
                              "events": reply["events"], "read_events": second["events"],
                              "client_value": nonce, "answer": second["answer"]}
        with mounted(work, server, host, config, "ro", request) as (opened, name, disconnect, _):
            server.run("docker", "exec", name, "sh", "-ec", "test ! -e .git/marker && test ! -e .agent-edits && ! touch forbidden")
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
    parser.add_argument("--scenario", choices=("all", "conflicts", "cli-conflict"), default="all")
    args = parser.parse_args()
    if not __debug__:
        parser.error("run without -O")
    result = verify(args.url, args.host, args.config, args.scenario)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S-%fZ")
    path = Path("results/sshfs") / ("workspace-" + stamp + ".json")
    path.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    print(path)


if __name__ == "__main__":
    main()
