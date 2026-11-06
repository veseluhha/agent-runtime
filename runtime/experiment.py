import argparse
import hashlib
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from .completions import Conversation
from .harness import run_task
from .http_api import Fault, post
from .tasks import TASKS

PROXY = "http://model-proxy:8080"
IDS = ("B1.1", "B2.1", "B3.1", "B4.1", "B5.1")


def task_digest(task):
    files = {p.relative_to(task.directory).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
             for p in sorted(task.directory.rglob("*")) if p.is_file() and "__pycache__" not in p.parts}
    return hashlib.sha256(json.dumps(files, sort_keys=True).encode()).hexdigest()


def write_line(stream, value):
    stream.write(json.dumps(value, ensure_ascii=False, allow_nan=False) + "\n")
    stream.flush()


def record_turn(stream, request, timeout):
    row = {"request": request}
    try:
        response = post(PROXY, "/v0/completion", {"request": request, "timeout_s": timeout}, timeout=timeout)
    except Fault as exc:
        row["error"] = {"kind": exc.kind, "message": str(exc)}
        raise
    else:
        row["response"] = response
        return response
    finally:
        write_line(stream, row)


def replay_turn(stream, request, timeout):
    line = stream.readline()
    if not line:
        raise Fault("replay", "recording ended before the next request")
    try:
        turn = json.loads(line)
        original = turn["request"]
        if "error" not in turn and "response" not in turn:
            raise KeyError("response")
    except (KeyError, TypeError, ValueError) as exc:
        raise Fault("replay", "invalid or incomplete recorded turn") from exc
    if original != request:
        raise Fault("replay", "request or tool output differs from the recording")
    if "error" in turn:
        raise Fault(turn["error"]["kind"], turn["error"]["message"])
    return turn["response"]


def run(mode, root, source_id=None, task_ids=IDS):
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S-%fZ")
    folder = root / mode / stamp
    if mode == "record":
        tasks = [TASKS[key] for key in task_ids]
        fingerprints = {task.id: task_digest(task) for task in tasks}
        model = post(PROXY, "/v0/model", {})["model"]
        source = folder
        meta = {"format": 1, "model": model, "tasks": fingerprints}
    else:
        if not re.fullmatch(r"[0-9]{8}T[0-9]{6}-[0-9]{6}Z", source_id or ""):
            raise ValueError("RUN must be the ID printed by make baf-record")
        source = root / "record" / source_id
        meta = json.loads((source / "manifest.json").read_text())
        tasks = [TASKS[key] for key in meta["tasks"]]
        fingerprints = {task.id: task_digest(task) for task in tasks}
        if meta["format"] != 1 or meta["tasks"] != fingerprints:
            raise ValueError("recording format or task files have changed")
        model = meta["model"]
        expected = {row["task_id"]: row for row in
                    map(json.loads, (source / "results.jsonl").read_text().splitlines())}
        if set(expected) != set(fingerprints):
            raise ValueError("recording incomplete: missing task results")
    folder.mkdir(parents=True, exist_ok=False)
    (folder / "manifest.json").write_text(json.dumps({**meta, "mode": mode, "source_run": source.name}, indent=2) + "\n")
    location = Path("results/baf") / mode / stamp
    print(f"{mode} | {model} | {location}", flush=True)
    passed = 0
    rows = []
    with (folder / "results.jsonl").open("x") as results:
        for task in tasks:
            started = time.monotonic()
            path = source / f"{task.id}.jsonl"
            with path.open("x" if mode == "record" else "r", encoding="utf-8") as stream:
                exchange = record_turn if mode == "record" else replay_turn
                chat = Conversation(model, lambda req, timeout: exchange(stream, req, timeout))
                try:
                    row = run_task(task, chat.next_action)
                except Fault as exc:
                    row = {"task_id": task.id, "repeat": 1, "pass": False, "reason": str(exc),
                           "outcome": exc.kind, "seconds": round(time.monotonic() - started, 4)}
                if mode == "replay":
                    fields = ("outcome", "pass", "answer_preview")
                    if stream.read().strip() or any(row.get(k) != expected[task.id].get(k) for k in fields):
                        row.update({"pass": False, "outcome": "replay_mismatch",
                                    "reason": "unused turns or result differs: " + row["reason"]})
                recorded_in = chat.tokens("prompt_tokens")
                recorded_out = chat.tokens("completion_tokens")
                row.update(pair="v0+" + model, mode=mode, response_models=chat.models,
                           tokens_in=recorded_in if mode == "record" else 0,
                           tokens_out=recorded_out if mode == "record" else 0)
                if mode == "replay":
                    row.update(recorded_tokens_in=recorded_in, recorded_tokens_out=recorded_out)
                row["recording"] = str(location)
                write_line(results, row)
                rows.append(row)
            passed += int(row["pass"])
            print(f"{task.id}  {'PASS' if row['pass'] else 'FAIL'}  {row['reason']}", flush=True)
    print(f"{passed}/{len(tasks)} | {location}/results.jsonl", flush=True)
    if mode == "record":
        print(f"make baf-replay RUN={stamp}", flush=True)
    return rows


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=["record", "replay"])
    args = parser.parse_args()
    try:
        rows = run(args.mode, Path("/results"), os.getenv("RUN"))
        return int(not all(row["pass"] for row in rows))
    except (Fault, ValueError, KeyError, OSError) as exc:
        sys.exit(str(exc))


if __name__ == "__main__":
    sys.exit(main())
