import re
from pathlib import Path

from _common import MATCH_CAP, GREP_FILE_CAP

def _is_text(path: Path) -> bool:
    try: return b"\x00" not in path.open("rb").read(1024)
    except OSError: return False

def glob(path: Path, args: dict) -> dict:
    pattern = args.get("pattern")
    if not pattern: return {"error": "pattern required"}
    if not path.is_dir(): return {"error": f"not a directory: {path}"}
    matches = sorted(str(m) for m in path.glob(pattern))
    return {"path": str(path), "matches": matches[:MATCH_CAP],
            "truncated": len(matches) > MATCH_CAP}

def grep(path: Path, args: dict) -> dict:
    pattern = args.get("pattern")
    if not pattern: return {"error": "pattern required"}
    try: rx = re.compile(pattern)
    except re.error as e: return {"error": f"bad regex: {e}"}
    files = [path] if path.is_file() else \
            [f for f in sorted(path.rglob(args.get("include", "*")))
             if f.is_file() and f.stat().st_size <= GREP_FILE_CAP and _is_text(f)]
    matches, truncated = [], False
    for f in files:
        for i, line in enumerate(f.read_text(errors="replace").splitlines(), 1):
            if rx.search(line):
                if len(matches) >= MATCH_CAP:
                    truncated = True
                    break
                matches.append({"file": str(f), "line": i, "text": line.strip()[:500]})
        if truncated: break
    return {"path": str(path), "matches": matches, "truncated": truncated}
