import json
import sys
from pathlib import Path

answer = Path(sys.argv[1]) / "answer.txt"
ok = answer.is_file() and answer.read_text().strip() == "src/lib/dates.py"
print(json.dumps({"pass": ok, "reason": "ok" if ok else "expected path to the definition"}))
