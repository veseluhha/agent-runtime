import json
import sys
from pathlib import Path

from runtime.harness import WRITE_FILE, fresh_sandbox
from runtime.http_api import Fault

CASES = {
    "Release  2.1!": "release-2-1",
    " /Alpha_BETA-709/ ": "alpha-beta-709",
    "éß AΩ0": "a-0",
    "K": "",
    "": "",
    "---": "",
}
PROBE = """import json, runpy, sys
slugify = runpy.run_path("slug.py")["slugify"]
print(json.dumps([slugify(text) for text in json.loads(sys.argv[1])]))
"""


def check(work):
    source = work / "slug.py"
    if not source.is_file():
        return False, "slug.py missing"
    with fresh_sandbox(15) as sandbox:
        sandbox.command(["python", "-I", "-c", WRITE_FILE, "slug.py", source.read_bytes().hex()], "setup")
        try:
            observed = sandbox.command(["python", "-I", "-S", "-c", PROBE, json.dumps(list(CASES))], "check")
            values = json.loads(observed["output"])
        except Fault as exc:
            if exc.kind not in ("command", "timeout", "output_limit"):
                raise
            return False, f"slugify failed: {exc.kind}"
        except ValueError:
            return False, "invalid output from slugify"
    ok = values == list(CASES.values())
    return ok, "ok" if ok else "hidden cases failed"


passed, reason = check(Path(sys.argv[1]))
print(json.dumps({"pass": passed, "reason": reason}))
