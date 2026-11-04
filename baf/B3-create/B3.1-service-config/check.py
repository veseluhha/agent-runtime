import json
import sys
from pathlib import Path

import fastjsonschema

schema = json.loads(Path(__file__).with_name("schema.json").read_text())
try:
    config = json.loads((Path(sys.argv[1]) / "config.json").read_text())
    fastjsonschema.validate(schema, config)
except (FileNotFoundError, ValueError, fastjsonschema.JsonSchemaException) as exc:
    verdict = {"pass": False, "reason": str(exc)}
else:
    verdict = {"pass": True, "reason": "ok"}
print(json.dumps(verdict))
