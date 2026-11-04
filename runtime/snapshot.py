"""Executed inside /work to return the final tree of a small smoke fixture."""
import base64
import json
from pathlib import Path


def main():
    entries = {}
    for path in sorted(Path(".").rglob("*")):
        if path.is_symlink() or not (path.is_dir() or path.is_file()):
            raise ValueError(f"unsupported file type: {path}")
        entries[str(path)] = None if path.is_dir() else base64.b64encode(path.read_bytes()).decode("ascii")
    print(json.dumps(entries))


if __name__ == "__main__":
    main()
