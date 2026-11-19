"""Exercise the workspace API from a client connected to a Linux Docker host."""
import argparse
import json
import os
import secrets
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from runtime.http_api import Fault, post
from runtime.mount import export_directory
from .probe import Server


def verify(url, host, config):
    server = Server(host, config)
    token = os.environ["CLIENT_TOKEN"]

    def request(operation, **body):
        return post(url, "/v0/workspaces/" + operation, body, token=token, timeout=95)

    project = Path(__file__).resolve().parents[2]
    fixture = project / "baf/B4-edit/B4.1-install-version/fixture/README.md"
    original = fixture.read_bytes()
    expected = original.replace(b"pip install orders-cli==1.2", b"pip install orders-cli==1.3", 1)
    result = {}
    with tempfile.TemporaryDirectory(prefix="workspace-client-") as tmp:
        work = Path(tmp)
        readme = work / "README.md"
        readme.write_bytes(original)
        (work / ".git").mkdir()
        (work / ".git/marker").write_text("metadata")
        for mode in ("rw", "ro"):
            with export_directory(work, host, config, mode) as mount_id:
                opened = request("open", mount_id=mount_id, access=mode)
                session = opened["session"]
                result["model"] = opened["model"]
                # Discover the sandbox by its random session suffix on this test host
                name = server.run("docker", "ps", "--filter", "name=-work-" + session + "$", "--format", "{{.Names}}")
                assert name and server.run("docker", "exec", name, "id", "-u") == "65534"
                try:
                    if mode == "rw":
                        reply = request("message", session=session, message=(
                            "Измени версию orders-cli на 1.3 только в разделе Install файла README.md. "
                            "Сохрани остальные байты. Используй временный файл и переименование."))
                        assert readme.read_bytes() == expected, "unexpected README bytes on client"
                        nonce = secrets.token_hex(8)
                        (work / "receipt.txt").write_text(nonce)
                        second = request("message", session=session, message="Прочитай receipt.txt и верни его содержимое")
                        assert nonce in (second["answer"] or "") and second["events"], "client edit not observed"
                        result["edit"] = {"before": original.decode(), "after": expected.decode(),
                                          "events": reply["events"], "read_events": second["events"],
                                          "client_value": nonce, "answer": second["answer"]}
                    else:
                        server.run("docker", "exec", name, "sh", "-ec",
                                   "test ! -e .git/marker && ! touch forbidden")
                        assert not (work / "forbidden").exists()
                        result["read_only"] = True
                finally:
                    if mode == "rw":
                        request("close", session=session)
            try:
                if mode == "ro":
                    try:
                        request("message", session=session, message="Прочитай README.md")
                    except Fault as exc:
                        if exc.kind != "mount_lost":
                            raise
                        result["disconnect"] = exc.kind
                    else:
                        raise AssertionError("disconnected workspace accepted a message")
            finally:
                request("close", session=session)
            server.run("test", "!", "-e", "/tmp/agent-runtime-" + mount_id)
            assert not server.run("docker", "ps", "-aq", "--filter", "name=-work-" + session + "$")
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
