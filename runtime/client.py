import argparse
import json
import os
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from .http_api import Fault, TransportError, post
from .mount import check_conflicts, workspace_mount


def run_suite(url, token, folder=Path("results/baf/live"), required=()):
    task_ids = post(url, "/v0/tasks", {}, token=token)["task_ids"]
    missing = [task for task in required if task not in task_ids]
    if missing:
        sys.exit("в реестре отсутствуют задачи ЧП1 -- " + ", ".join(missing))
    if required:
        task_ids = required
    if not task_ids:
        sys.exit("no simple tasks registered")
    folder.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S-%fZ")
    path = folder / f"{stamp}.jsonl"
    passed = 0
    print("smoke | live model", flush=True)
    with path.open("x", encoding="utf-8") as stream:
        for task_id in task_ids:
            start = time.monotonic()
            try:
                row = post(url, "/v0/smoke", {"task_id": task_id}, token=token, timeout=200)
            except Fault as exc:
                row = {"task_id": task_id, "repeat": 1, "pair": None, "pass": False,
                       "outcome": exc.kind, "reason": str(exc), "seconds": round(time.monotonic() - start, 4)}
            stream.write(json.dumps(row, ensure_ascii=False) + "\n")
            stream.flush()
            passed += int(row["pass"])
            status = "PASS" if row["pass"] else "FAIL"
            print(f"{task_id}  {status}  {row['seconds']:.2f} s  {row['reason']}", flush=True)
            if row.get("pair"):
                print(f"    {row['pair']} | {row.get('recording', '')}", flush=True)
            if row.get("answer_preview"):
                for line in row["answer_preview"].rstrip().splitlines():
                    print("    " + line, flush=True)
    print(f"{passed}/{len(task_ids)} | {path}")
    return int(passed != len(task_ids))


def send_message(url, token, session, prompt):
    key = {**session, "message_id": uuid.uuid4().hex}
    body = {**key, "message": prompt}
    deadline = time.monotonic() + 110
    submit = True
    recovering = False
    delay = 0.25
    while (remaining := deadline - time.monotonic()) > 0:
        try:
            if submit:
                return post(url, "/v0/workspaces/message", body, token=token, timeout=min(95, remaining))
            state = post(url, "/v0/workspaces/status", key, token=token, timeout=min(5, remaining))
        except Fault as exc:
            if not isinstance(exc, TransportError) and exc.kind != "in_progress":
                if recovering and exc.kind == "session":
                    raise Fault("unconfirmed", "сессия завершена, исход сообщения неизвестен", 410) from exc
                raise
            if not recovering:
                print("восстанавливаю ответ на сообщение " + key["message_id"], file=sys.stderr, flush=True)
                recovering = True
            submit = False
        else:
            if state["state"] == "completed":
                return state["result"]
            if state["state"] == "failed":
                raise Fault(state["kind"], state["message"], state["status"])
            if state["state"] not in ("not_received", "running"):
                raise ValueError("unknown workspace message state")
            submit = state["state"] == "not_received"
        time.sleep(min(delay, max(0, deadline - time.monotonic())))
        delay = min(2, delay * 2)
    raise Fault("unconfirmed", "истекло время ожидания, исход сообщения неизвестен", 504)


def chat(url, token, args):
    access = "rw" if args.write else "ro"
    with workspace_mount(args.workdir, args.ssh_host, args.ssh_config, access) as (mount_id, edits):
        opened = post(url, "/v0/workspaces/open", {"mount_id": mount_id, "access": access}, token=token)
        session = {"session": opened["session"]}
        print(f"{opened['model']} | {access} | {session['session']}", flush=True)
        try:
            prompts = iter(args.message) if args.message else iter(lambda: input("> "), "")
            for turn, prompt in enumerate(prompts):
                if edits and turn:
                    edits.refresh()
                result = send_message(url, token, session, prompt)
                for event in result["events"]:
                    print(f"{event['exit_code']} | {event['argv']}", flush=True)
                if edits:
                    check_conflicts(edits.publish())
                print(result["answer"] or "", flush=True)
        except EOFError:
            pass
        finally:
            failed = sys.exc_info()[0] is not None
            try:
                post(url, "/v0/workspaces/close", session, token=token)
            except Fault as exc:
                if not failed:
                    raise
                print(f"закрытие сессии не подтверждено -- {exc}", file=sys.stderr)
    return 0


def main():
    parser = argparse.ArgumentParser(description="Run benchmark tasks or open a client workspace")
    parser.add_argument("--url", default="http://127.0.0.1:" + os.getenv("GATEWAY_PORT", "8080"))
    selection = parser.add_mutually_exclusive_group()
    selection.add_argument("--all", action="store_true")
    selection.add_argument("--checkpoint1", action="store_true")
    selection.add_argument("--task", default="B1.1")
    selection.add_argument("--workdir", type=Path, help="open a folder on this machine")
    parser.add_argument("--ssh-host", help="SSH host running Docker and SSHFS")
    parser.add_argument("--ssh-config", type=Path, help="OpenSSH config for the mount connection")
    parser.add_argument("--write", action="store_true", help="allow changes to the mounted folder")
    parser.add_argument("--message", action="append", help="send a prompt (repeatable); default: interactive")
    args = parser.parse_args()
    if args.workdir and not args.ssh_host:
        parser.error("--workdir requires --ssh-host")
    if not args.workdir and (args.ssh_host or args.ssh_config or args.write or args.message):
        parser.error("workspace options require --workdir")
    url = args.url.rstrip("/")
    token = os.environ["CLIENT_TOKEN"]
    try:
        if args.workdir:
            return chat(url, token, args)
        if args.checkpoint1:
            return run_suite(url, token, required=("B1.1", "B2.1", "B3.1", "B4.1", "B5.1"))
        if args.all:
            return run_suite(url, token)
        result = post(url, "/v0/smoke", {"task_id": args.task}, token=token, timeout=200)
    except Fault as exc:
        sys.exit(f"{exc.kind}: {exc}")
    except KeyboardInterrupt:
        return 130
    except (OSError, ValueError, RuntimeError) as exc:
        sys.exit(str(exc))
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result["pass"] else 1


if __name__ == "__main__":
    sys.exit(main())
