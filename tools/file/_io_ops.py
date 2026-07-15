from pathlib import Path

from _common import READ_CAP, read_files

def read(path: Path, args: dict) -> dict:
    if not path.is_file(): return {"error": f"not a file: {path}"}
    lines = path.read_text(errors="replace").splitlines()
    start, end = args.get("start", 1), args.get("end", len(lines))
    if start < 1 or start > end: return {"error": f"bad range: {start}-{end}"}
    chunk = lines[start - 1:end]
    text = "\n".join(f"{i}\t{l}" for i, l in enumerate(chunk, start))
    read_files.add(str(path))
    return {"path": str(path), "content": text[:READ_CAP],
            "lines": len(lines), "truncated": len(text) > READ_CAP}

def write(path: Path, args: dict) -> dict:
    content = args.get("content", "")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    read_files.add(str(path))
    return {"path": str(path), "written": len(content)}

def edit(path: Path, args: dict) -> dict:
    if not path.is_file(): return {"error": f"not a file: {path}"}
    if str(path) not in read_files:
        return {"error": "read the file before editing it"}
    edits = args.get("edits")
    if not edits: return {"error": "edits required"}
    text = path.read_text()
    for i, e in enumerate(edits):
        old, new = e.get("old"), e.get("new", "")
        if not old: return {"error": f"edit {i}: old required"}
        n = text.count(old)
        if n == 0: return {"error": f"edit {i}: old string not found"}
        if n > 1 and not e.get("all"):
            return {"error": f"edit {i}: old string matches {n} times, make it unique or set all: true"}
        text = text.replace(old, new)
    path.write_text(text)
    return {"path": str(path), "edits": len(edits)}

def mkdir(path: Path, args: dict) -> dict:
    path.mkdir(parents=True, exist_ok=True)
    return {"created": str(path)}
