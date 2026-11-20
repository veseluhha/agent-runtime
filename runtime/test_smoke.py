import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from .harness import WRITE_FILE, restore, run_task
from .http_api import Fault
from .tasks import TASKS


def check(task_id, name, content):
    with tempfile.TemporaryDirectory() as tmp:
        (Path(tmp) / name).write_bytes(content)
        output = subprocess.check_output([sys.executable, str(TASKS[task_id].directory / "check.py"), tmp])
    return json.loads(output)["pass"]


class SmokeControls(unittest.TestCase):
    def test_checkers_reject_wrong_answers(self):
        readme = (TASKS["B4.1"].directory / "fixture/README.md").read_bytes()
        for task_id, name, wrong in [
            ("B1.1", "answer.txt", b"8080"),
            ("B2.1", "answer.txt", b"src/report.py"),
            ("B3.1", "config.json", b'{"service":"orders","retries":"3","debug":false}'),
            ("B4.1", "README.md", readme.replace(b"==1.2", b"==1.3")),
            ("B5.1", "answer.txt", b"16"),
            ("B7.1", "answer.txt", b"yes 13"),
            ("B7.1", "answer.txt", b"no 8"),
            ("B8.1", "answer.txt", b"367.50"),
        ]:
            with self.subTest(task_id=task_id):
                self.assertFalse(check(task_id, name, wrong))

    def test_collector_rejects_parent_path(self):
        with tempfile.TemporaryDirectory() as tmp, self.assertRaises(Fault):
            restore({"../escape": "YWJj"}, Path(tmp))

    def test_agent_can_reason_about_a_failed_command(self):
        observations = []

        def next_action(history, timeout):
            if history[-1]["role"] == "user":
                return {"commands": [["sh", "-c", "echo OK; echo build-failed >&2; exit 1"]]}
            observations.append(history[-1])
            return {"done": True}

        result = run_task(TASKS["B1.1"], next_action)
        self.assertEqual(len(observations), 1)
        self.assertEqual(observations[0]["exit_code"], 1)
        self.assertCountEqual(observations[0]["output"].splitlines(), ["OK", "build-failed"])
        self.assertEqual(result["outcome"], "completed")
        self.assertFalse(result["pass"])  # no answer.txt

    def test_every_simple_task_runs_offline(self):
        transfer = """import csv
from datetime import datetime
from pathlib import Path
with open('schedule.csv') as source:
    stops = {r['train']: r for r in csv.DictReader(source) if r['station'] == 'Y'}
minutes = (datetime.fromisoformat(stops['T19']['departure']) -
           datetime.fromisoformat(stops['T12']['arrival'])).total_seconds() // 60
Path('answer.txt').write_text(f"{'yes' if minutes >= 15 else 'no'} {int(minutes)}")
"""
        quarter = """import csv
from decimal import Decimal
from pathlib import Path
total = Decimal(0)
for file in Path('sales').glob('*.csv'):
    with file.open() as source:
        total += sum(Decimal(row['amount']) for row in csv.DictReader(source)
                     if '2026-01-01' <= row['date'] < '2026-04-01')
Path('answer.txt').write_text(str(total))
"""
        slug = 'import re\ndef slugify(text):\n    return re.sub("[^a-zA-Z0-9]+", "-", text).strip("-").lower()\n'
        commands = {
            "B1.1": ["sh", "-ec", "awk '$1 == \"port:\" {print $2}' config/app.yaml > answer.txt"],
            "B2.1": ["sh", "-ec", "grep -rl '^def parse_date(' src > answer.txt"],
            "B3.1": ["python", "-c", """import json, pathlib
lines = pathlib.Path('spec.md').read_text().splitlines()
rows = [line.strip('|').split('|') for line in lines if line.startswith('|')]
pathlib.Path('config.json').write_text(json.dumps({k.strip(): json.loads(v) for k,v in rows[2:]}))
"""],
            "B4.1": ["sed", "-i", "/^## Install/,/^## Reproduce/s/orders-cli==1.2/orders-cli==1.3/", "README.md"],
            "B5.1": ["sh", "-ec", "python stats.py data.csv > answer.txt"],
            "B6.1": ["python", "-I", "-c", WRITE_FILE, "slug.py", slug.encode().hex()],
            "B7.1": ["python", "-c", transfer],
            "B8.1": ["python", "-c", quarter],
        }
        for number in range(1, 9):
            task_id = f"B{number}.1"
            with self.subTest(task_id=task_id):
                def next_action(history, timeout):
                    if history[-1]["role"] == "user":
                        return {"commands": [commands[task_id]]}
                    self.assertEqual(history[-1]["exit_code"], 0, history[-1]["output"])
                    return {"done": True}

                row = run_task(TASKS[task_id], next_action)
                self.assertTrue(row["pass"], row["reason"])
                print(f"offline smoke | {task_id} PASS", flush=True)

    def test_slug_checker_rejects_a_partial_implementation(self):
        source = b"def slugify(text):\n    return text.lower().replace(' ', '-')"

        def next_action(history, timeout):
            if history[-1]["role"] == "user":
                return {"commands": [["python", "-I", "-c", WRITE_FILE, "slug.py", source.hex()]]}
            return {"done": True}
        row = run_task(TASKS["B6.1"], next_action)
        self.assertFalse(row["pass"])
        self.assertEqual(row["reason"], "hidden cases failed")
