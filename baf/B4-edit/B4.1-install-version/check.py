import json
import sys
from pathlib import Path

original = (Path(__file__).parent / "fixture/README.md").read_bytes()
expected = original.replace(b"orders-cli==1.2", b"orders-cli==1.3", 1)
work = Path(sys.argv[1])
target = work / "README.md"
ok = set(work.iterdir()) == {target} and target.is_file() and target.read_bytes() == expected
print(json.dumps({"pass": ok, "reason": "ok" if ok else "only the Install line may change"}))
