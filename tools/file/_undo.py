import json, os, tempfile
from pathlib import Path

import _common

INDEX = "index.jsonl"
KEEP = 200            # snapshots retained; the oldest are dropped, not the newest
SNAP_MAX = 20_000_000 # bytes. Past this, no snapshot and so no undo, rather than
                      # a session directory quietly filling a disk

def _dir(ctx) -> Path:
    """Where the snapshots live.

    The session's own directory when the frontend reports one, so a run's undo
    history is thrown out with the run. A frontend that reports none still gets
    undo, from a fixed directory under home, rather than the feature quietly
    existing or not depending on who launched the app.
    """
    base = getattr(ctx, "session_dir", None) if ctx is not None else None
    d = Path(base) / "file_undo" if base else Path.home() / ".cuacode" / "file_undo"
    d.mkdir(parents=True, exist_ok=True)
    return d

def _entries(d: Path) -> list[dict]:
    try: text = (d / INDEX).read_text(errors="replace")
    except OSError: return []
    out = []
    for line in text.splitlines():
        try: out.append(json.loads(line))
        except ValueError: continue      # a half-written line from a killed process
    return out

def record(ctx, path: Path, kind: str, before: bytes | None = None, src: Path | None = None) -> None:
    """Remember what `path` was, just before it stops being that.

    Every failure here is swallowed. Undo is a safety net stretched under the
    edit, not a condition of it, and a full disk is no reason to refuse a write
    the user asked for.
    """
    if before is not None and len(before) > SNAP_MAX: return
    try:
        d = _dir(ctx)
        seq = max([e.get("seq", 0) for e in _entries(d)], default=0) + 1
        snap = None
        if before is not None:
            snap = f"{seq:06d}.snap"
            (d / snap).write_bytes(before)
        entry = {"seq": seq, "kind": kind, "path": str(path), "snap": snap}
        if src is not None: entry["src"] = str(src)
        with (d / INDEX).open("a") as f: f.write(json.dumps(entry) + "\n")
        _prune(d)
    except OSError:
        pass

def undo(path: Path, args: dict, ctx=None) -> dict:
    """Put the file back the way it was before the last thing done to it.

    One step, and only for the path named. An undo that walked backwards through
    everything the agent touched would be a second way to change files nobody
    asked it to change.
    """
    d = _dir(ctx)
    entries = _entries(d)
    spent = {e["undo"] for e in entries if "undo" in e}
    live = [e for e in entries
            if e.get("seq") not in spent and e.get("path") == str(path)]
    if not live: return {"error": f"nothing to undo for {path}"}
    e = live[-1]

    try:
        if e["kind"] == "create":
            path.unlink(missing_ok=True)
            _forget(path)
            was = "the file it created was removed"
        elif e["kind"] == "move":
            src = Path(e["src"])
            if src.exists(): return {"error": f"cannot move it back: {src} exists again"}
            src.parent.mkdir(parents=True, exist_ok=True)
            os.replace(path, src)
            _forget(path)
            was = f"moved back to {src}"
        else:
            _write_bytes(path, (d / e["snap"]).read_bytes())
            _common.record(path, _common.load(path)["meta"])
            was = f"restored the content from before the {e['kind']}"
    except OSError as err:
        return {"error": f"undo failed: {err}"}

    with (d / INDEX).open("a") as f: f.write(json.dumps({"undo": e["seq"]}) + "\n")
    return {"path": str(path), "undone": e["kind"], "note": was}

def _write_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as f: f.write(data)
        os.chmod(tmp, 0o644)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise

def _forget(path: Path) -> None:
    """A path that no longer holds what was read from it is not a read file."""
    _common.read_files.discard(str(path))
    _common.seen.pop(str(path), None)

def _prune(d: Path) -> None:
    snaps = sorted(d.glob("*.snap"))
    for old in snaps[:-KEEP]: old.unlink(missing_ok=True)
