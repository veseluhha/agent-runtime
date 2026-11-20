import json
import sys
from decimal import Decimal, InvalidOperation
from pathlib import Path

try:
    total = Decimal((Path(sys.argv[1]) / "answer.txt").read_text().strip())
    ok = total.is_finite() and total == Decimal("177.50")
except (OSError, ValueError, InvalidOperation):
    ok = False
print(json.dumps({"pass": ok, "reason": "ok" if ok else "expected Q1 net sales"}))
