import hmac
import os

from .harness import run_smoke
from .http_api import Fault, serve
from .tasks import TASKS


def dispatch(path, body, headers):
    if not hmac.compare_digest(headers.get("Authorization", "").encode(),
                               ("Bearer " + os.environ["CLIENT_TOKEN"]).encode()):
        raise Fault("unauthorized", "invalid token", 401)
    if path == "/v0/tasks" and body == {}:
        return {"task_ids": list(TASKS)}
    if path != "/v0/smoke":
        raise Fault("unknown", "unknown endpoint", 404)
    task_id = body.get("task_id")
    if set(body) != {"task_id"} or not isinstance(task_id, str) or task_id not in TASKS:
        raise Fault("invalid", "unknown or invalid task_id")
    return run_smoke(TASKS[task_id])


if __name__ == "__main__":
    serve(dispatch)
