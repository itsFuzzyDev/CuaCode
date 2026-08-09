from pathlib import Path

import _common, _diff, _undo, _verify
from _common import READ_CAP, READ_LINES

LOSSY = "not valid UTF-8 -- writing it back would destroy the bytes shown as �"

def read(path: Path, args: dict, ctx=None) -> dict:
    if path.is_dir(): return {"error": f"is a directory: {path} -- use glob or grep"}
    if not path.is_file(): return {"error": f"not a file: {path}"}
    f = _common.load(path)
    lines = f["text"].split("\n")
    # A trailing newline terminates the last line, it does not begin an empty one.
    if lines and lines[-1] == "": lines.pop()
    total = len(lines)
    if not total:
        _common.record(path, f["meta"])
        return {"path": str(path), "content": "", "lines": 0, "truncated": False}
    start, end = args.get("start", 1), args.get("end", total)
    if start < 1: return {"error": f"bad range: start is {start}, lines are 1-based"}
    if start > total: return {"error": f"start {start} is past the end ({total} lines)"}
    end = min(end, total)
    if end < start: return {"error": f"bad range: {start}-{end}"}

    out, used = [], 0
    for i, l in enumerate(lines[start - 1:min(end, start - 1 + READ_LINES)], start):
        row = f"{i}\t{l}"
        if used + len(row) + 1 > READ_CAP: break
        out.append(row); used += len(row) + 1
    # One line longer than the whole budget would otherwise return nothing and ask
    # the model to resume from where it already was, forever.
    if not out: out = [f"{start}\t{lines[start - 1][:READ_CAP]}"]

    _common.record(path, f["meta"])
    stop = start + len(out)
    res = {"path": str(path), "content": "\n".join(out), "lines": total,
           "truncated": stop <= end}
    if res["truncated"]: res["next_start"] = stop
    if f["meta"]["lossy"]: res["lossy"] = True; res["note"] = f"{path.name} is {LOSSY}"
    return res

def write(path: Path, args: dict, ctx=None) -> dict:
    content = args.get("content")
    if content is None: return {"error": "content required"}
    if path.is_dir(): return {"error": f"is a directory: {path}"}

    meta, before = {}, None
    if path.exists():
        # The same gate edit uses. Overwriting is the more destructive of the two
        # -- an edit that misses fails, a write that misses takes the file with it.
        if str(path) not in _common.read_files:
            return {"error": f"read {path} before overwriting it"}
        if msg := _common.stale(path): return {"error": msg}
        old = _common.load(path)
        if old["meta"]["lossy"]: return {"error": f"{path} is {LOSSY}"}
        meta, before = old["meta"], old["text"]
        if before == content:
            return {"path": str(path), "unchanged": True}
        _undo.record(ctx, path, "write", before=old["raw"])
    else:
        _undo.record(ctx, path, "create")

    _common.record(path, _common.save(path, content, meta))
    res = {"path": str(path), "written": len(content)}
    if before is None: res["created"] = True
    else: res["diff"] = _diff.unified(before, content, path.name)
    if err := _verify.check(path, content): res["syntax_error"] = err
    return res

def edit(path: Path, args: dict, ctx=None) -> dict:
    if not path.is_file(): return {"error": f"not a file: {path}"}
    if str(path) not in _common.read_files: return {"error": f"read {path} before editing it"}
    if msg := _common.stale(path): return {"error": msg}
    edits = args.get("edits")
    if not edits: return {"error": "edits required"}
    f = _common.load(path)
    if f["meta"]["lossy"]: return {"error": f"{path} is {LOSSY}"}

    before = text = f["text"]
    for i, e in enumerate(edits):
        old, new = e.get("old"), e.get("new", "")
        if not old: return {"error": f"edit {i}: old required"}
        n = text.count(old)
        # Nothing is written until every edit has landed, so a batch that fails
        # halfway leaves the file as it was rather than half rewritten.
        if n == 0: return {"error": f"edit {i}: old string not found", **_repair(text, old)}
        if n > 1 and not e.get("all"):
            return {"error": f"edit {i}: old string matches {n} times, "
                             "add surrounding context to make it unique or set all: true"}
        text = text.replace(old, new)

    if text == before: return {"path": str(path), "edits": len(edits), "unchanged": True}
    _undo.record(ctx, path, "edit", before=f["raw"])
    _common.record(path, _common.save(path, text, f["meta"]))
    res = {"path": str(path), "edits": len(edits),
           "diff": _diff.unified(before, text, path.name)}
    if err := _verify.check(path, text): res["syntax_error"] = err
    return res

def mkdir(path: Path, args: dict, ctx=None) -> dict:
    path.mkdir(parents=True, exist_ok=True)
    return {"created": str(path)}

def _repair(text: str, old: str) -> dict:
    """What the model probably meant, when the exact string is not in the file.

    Nearly every failed edit is whitespace: the block was retyped from memory
    instead of copied, and lost an indent level on the way. Finding it again with
    indentation ignored, and handing back the file's own bytes for it, turns a
    guess-and-retry loop into one more call.

    Never applied on its own. Looking alike is not being the same, and an edit is
    the last place to start guessing.
    """
    want = [l.strip() for l in old.split("\n")]
    while want and not want[-1]: want.pop()
    lines = text.split("\n")
    if not want or len(want) > 200 or len(lines) > 20_000: return {}
    flat = [l.strip() for l in lines]
    hits = [i for i in range(len(flat) - len(want) + 1) if flat[i:i + len(want)] == want]
    if not hits: return {}
    if len(hits) > 1:
        return {"hint": f"{len(hits)} places match apart from whitespace "
                        f"(lines {', '.join(str(h + 1) for h in hits[:5])})"}
    i = hits[0]
    return {"did_you_mean": {"line": i + 1, "text": "\n".join(lines[i:i + len(want)])},
            "hint": "only whitespace differs -- pass did_you_mean.text verbatim as old"}
