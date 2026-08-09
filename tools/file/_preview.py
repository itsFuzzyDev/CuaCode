from pathlib import Path

import _common, _diff, _fs_ops

def build(args: dict, path: Path) -> dict | None:
    """What this call would do, in the terms the person approving it needs.

    A permission dialog showing {"action": "edit", "edits": [{"old": ...}]} asks
    the user to diff two strings in their head. Showing the patch instead is most
    of the difference between a prompt that gets read and one that gets clicked
    through.

    Read-only by construction: this runs before anyone has said yes, so it opens
    files and touches nothing -- not the disk, not the edit gate.
    """
    action = args.get("action")
    if action == "write": return _write(args, path)
    if action == "edit": return _edit(args, path)
    if action == "read":
        span = f" lines {args['start']}-{args.get('end', 'end')}" if args.get("start") else ""
        return {"summary": f"read {path}{span}"}
    if action == "delete":
        return {"summary": f"delete {path}" +
                           (" (to the Trash, recoverable)" if _fs_ops.TRASH else "")}
    if action == "move": return {"summary": f"move {path} to {args.get('to')}"}
    if action == "undo": return {"summary": f"roll back the last change to {path}"}
    if action == "mkdir": return {"summary": f"create directory {path}"}
    if action == "ls": return {"summary": f"list {path}"}
    if action in ("glob", "grep"):
        return {"summary": f"{action} {args.get('pattern')!r} under {path}"}
    return None

def _write(args: dict, path: Path) -> dict:
    content = args.get("content") or ""
    n = content.count("\n") + (0 if content.endswith("\n") else 1)
    if not path.exists(): return {"summary": f"create {path} ({n} lines)"}
    try: before = _common.load(path)["text"]
    except OSError: return {"summary": f"overwrite {path}"}
    return {"summary": f"overwrite {path} ({n} lines)",
            "diff": _diff.unified(before, content, path.name)}

def _edit(args: dict, path: Path) -> dict:
    edits = args.get("edits") or []
    try: before = _common.load(path)["text"]
    except OSError: return {"summary": f"edit {path}"}
    text = before
    for i, e in enumerate(edits):
        old = e.get("old")
        # The same refusals edit itself makes, reported as what would happen
        # rather than what did. Approving a call that cannot land is a worse
        # surprise than being told now.
        if not old or old not in text:
            return {"summary": f"edit {path}: edit {i} does not match -- nothing would be written"}
        if text.count(old) > 1 and not e.get("all"):
            return {"summary": f"edit {path}: edit {i} is ambiguous -- nothing would be written"}
        text = text.replace(old, e.get("new", ""))
    what = "1 replacement" if len(edits) == 1 else f"{len(edits)} replacements"
    return {"summary": f"edit {path} ({what})", "diff": _diff.unified(before, text, path.name)}
