import json
import re

from .http_api import Fault, serve

FIRST_CALL = {
    "B1.1": ["cat", "config/app.yaml"],
    "B2.1": ["grep", "-rlE", r"^[[:space:]]*def parse_date\(", "."],
    "B3.1": ["cat", "spec.md"],
    "B4.1": ["cat", "README.md"],
    "B5.1": ["python", "stats.py", "data.csv"],
}


def make_answer(task_id, output):
    if task_id == "B1.1":
        match = re.search(r"^\s*port:\s*([0-9]+)\s*$", output, re.MULTILINE)
        if not match:
            raise Fault("invalid", "fixture has no port")
        return "answer.txt", match[1] + "\n"
    if task_id == "B2.1":
        return "answer.txt", output.removeprefix("./")
    if task_id == "B3.1":
        rows = re.findall(r"^\| (\w+) \| (.+?) \|$", output, re.MULTILINE)
        fields = {key: json.loads(value) for key, value in rows if key != "Поле"}
        return "config.json", json.dumps(fields, indent=2) + "\n"
    if task_id == "B4.1":
        head, section = output.split("## Install\n", 1)
        body, heading, tail = section.partition("\n## ")
        return "README.md", head + "## Install\n" + body.replace("==1.2", "==1.3", 1) + heading + tail
    return "answer.txt", output


def dispatch(path, body, headers):
    if path != "/v0/next":
        raise Fault("unknown", "unknown endpoint", 404)
    model = body["model"]
    task_id = "B1.1" if model == "scripted-b1" else body.get("task_id")
    if model not in ("scripted-b1", "scripted-smoke") or task_id not in FIRST_CALL:
        raise Fault("invalid", "unsupported scripted task")
    history = body["messages"]
    outputs = [m["output"] for m in history if m["role"] == "tool"]
    if not outputs:
        return {"argv": FIRST_CALL[task_id]}
    if len(outputs) == 1:
        filename, content = make_answer(task_id, outputs[0])
        return {"argv": ["sh", "-c", 'printf "%s" "$2" > "$1"', "write-answer", filename, content]}
    return {"done": True}


if __name__ == "__main__":
    serve(dispatch)
