import json
import sys
from pathlib import Path

path = Path(sys.argv[1]) / "answer.txt"
ok = path.is_file() and path.read_bytes().split() == [b"no", b"13"]
print(json.dumps({"pass": ok, "reason": "ok" if ok else "expected the decision and minutes available"}))
