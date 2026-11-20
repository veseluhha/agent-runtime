from dataclasses import dataclass
from pathlib import Path

import yaml


@dataclass
class Task:
    id: str
    directory: Path
    messages: list
    answer_file: str
    timeout_s: int
    checker_timeout_s: int = 5


def load_tasks(root):
    tasks = {}
    for manifest in sorted(root.glob("*/*/task.yaml")):
        data = yaml.safe_load(manifest.read_text())
        if data["level"] != "simple":
            continue
        task = Task(data["id"], manifest.parent, data["messages"],
                    data["answer_file"], data["timeout_s"], data.get("checker_timeout_s", 5))
        answer = Path(task.answer_file)
        if task.id in tasks or answer.is_absolute() or ".." in answer.parts:
            raise ValueError(f"invalid task manifest: {manifest}")
        tasks[task.id] = task
    return tasks


TASKS = load_tasks(Path(__file__).resolve().parents[1] / "baf")
