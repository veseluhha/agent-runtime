import json
import sys
from decimal import Decimal, InvalidOperation
from pathlib import Path

try:
    number = Decimal((Path(sys.argv[1]) / "answer.txt").read_text().strip())
    ok = number.is_finite() and number == Decimal("13.5")
except (FileNotFoundError, InvalidOperation, ValueError):
    ok = False
print(json.dumps({"pass": ok, "reason": "ok" if ok else "expected the median of data.csv"}))
