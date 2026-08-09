import os, platform, shutil
from pathlib import Path

import _common, _undo

LS_CAP = 200
TRASH = Path.home() / ".Trash" if platform.system() == "Darwin" else None

def ls(path: Path, args: dict, ctx=None) -> dict:
    if not path.exists(): return {"error": f"not found: {path}"}
    if path.is_file(): return {"path": str(path), "entries": [_entry(path)],
                               "count": 1, "truncated": False}
    try: kids = sorted(path.iterdir(), key=lambda p: (p.is_file(), p.name.lower()))
    except OSError as e: return {"error": str(e)}
    return {"path": str(path), "entries": [_entry(p) for p in kids[:LS_CAP]],
            "count": len(kids), "truncated": len(kids) > LS_CAP}

def delete(path: Path, args: dict, ctx=None) -> dict:
    if not path.exists(): return {"error": f"not found: {path}"}
    if path.is_dir():
        # Only an empty one. Recursively deleting a directory is the operation
        # this tool would most regret getting wrong, and it is one shell call away
        # for a user who means it.
        try: path.rmdir()
        except OSError:
            return {"error": f"{path} is a directory with contents -- "
                             "delete its files first, or use the shell tool"}
        return {"deleted": str(path)}
    try: before = path.read_bytes()
    except OSError as e: return {"error": str(e)}

    _undo.record(ctx, path, "delete", before=before)
    where = None
    if TRASH and TRASH.is_dir():
        # The Trash as well as the snapshot, because only one of the two is
        # somewhere the user can look without asking the agent for it back.
        try: where = str(shutil.move(str(path), str(_free(TRASH / path.name))))
        except (OSError, shutil.Error): where = None
    if where is None:
        try: path.unlink()
        except OSError as e: return {"error": str(e)}
    _common.read_files.discard(str(path))
    _common.seen.pop(str(path), None)
    return {"deleted": str(path), "trash": where, "undo": "file undo restores it"}

def move(path: Path, args: dict, ctx=None) -> dict:
    raw = args.get("to")
    if not raw: return {"error": "to required"}
    if not path.exists(): return {"error": f"not found: {path}"}
    dest = _common.resolve({"path": raw}, ctx)
    if dest == path: return {"path": str(path), "unchanged": True}
    # No clobbering. A move that lands on an existing file is either a rename the
    # agent got wrong or a delete it did not say it was doing.
    if dest.exists(): return {"error": f"{dest} already exists"}
    dest.parent.mkdir(parents=True, exist_ok=True)
    try: shutil.move(str(path), str(dest))
    except (OSError, shutil.Error) as e: return {"error": str(e)}

    # The gate follows the file: it is the same content the agent already read,
    # and making it re-read the file it just renamed teaches it nothing.
    if str(path) in _common.read_files:
        _common.read_files.discard(str(path))
        meta = _common.seen.pop(str(path), None)
        _common.read_files.add(str(dest))
        if meta:
            try: _common.seen[str(dest)] = {**meta, "mtime_ns": dest.stat().st_mtime_ns}
            except OSError: pass
    _undo.record(ctx, dest, "move", src=path)
    return {"moved": str(path), "to": str(dest)}

def _entry(p: Path) -> dict:
    try: st = p.stat()
    except OSError: return {"name": p.name, "type": "unknown"}
    if p.is_dir(): return {"name": p.name, "type": "dir"}
    return {"name": p.name, "type": "file", "size": st.st_size}

def _free(dest: Path) -> Path:
    """A name nothing is using, so trashing b.py twice keeps both."""
    if not dest.exists(): return dest
    for n in range(1, 1000):
        alt = dest.with_name(f"{dest.stem} {n}{dest.suffix}")
        if not alt.exists(): return alt
    return dest.with_name(f"{dest.stem} {os.getpid()}{dest.suffix}")
