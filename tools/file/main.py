import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))

import _fs_ops
import _io_ops
import _preview
import _search
import _undo
from _common import resolve

from tools._safety import paths

ACTIONS = {
    "read": _io_ops.read,
    "write": _io_ops.write,
    "edit": _io_ops.edit,
    "mkdir": _io_ops.mkdir,
    "glob": _search.glob,
    "grep": _search.grep,
    "ls": _fs_ops.ls,
    "delete": _fs_ops.delete,
    "move": _fs_ops.move,
    "undo": _undo.undo,
}

def run(args: dict, ctx) -> dict:
    handler = ACTIONS.get(args.get("action"))
    if not handler: return {"error": f"unknown action: {args.get('action')}"}
    # ctx carries the directory the session was launched from, which is the only
    # thing that makes a relative path mean what the user typed, and the session
    # directory undo keeps its snapshots in.
    try: path = resolve(args, ctx)
    except ValueError as e: return {"error": str(e)}
    return handler(path, args, ctx)

# The actions that only look. Everything else -- write, edit, delete, move,
# mkdir, undo -- changes the disk and is asked about however ordinary it looks.
READ_ONLY = {"read", "ls", "glob", "grep"}

def safe(args: dict, ctx) -> bool:
    """Whether this call can skip the permission prompt.

    Sensitivity is the floor: a path whose contents are themselves the thing
    being protected is asked about whatever is being done to it, because
    reading is harmless right up until the file is ~/.ssh/id_rsa and a check
    that only looked at the verb would hand that over without a word. Above
    that floor, either the action only reads, or it happens somewhere that is
    not the user's -- see below.

    The path is resolved first, so ../ and ~ are judged as where they land
    rather than as what they were typed as.
    """
    try: path = resolve(args, ctx)
    except ValueError: return False
    if paths.is_sensitive(path): return False
    if args.get("action") in READ_ONLY: return True
    # Third condition, and only for one place: the session's own scratch
    # directory. A write there is not the user's file being changed, so the
    # verb stops being the thing that matters. A move is asked about unless
    # both ends are inside -- otherwise it is a write to wherever `to` points,
    # wearing a scratch path as its source.
    if not paths.is_scratch(path, ctx): return False
    if args.get("action") == "move":
        return bool(args.get("to")) and paths.is_scratch(resolve({"path": args["to"]}, ctx), ctx)
    return True

def preview(args: dict, ctx) -> dict | None:
    """What run() would do, for the permission prompt to show before it happens.

    Called by the loop instead of the handler, so it must not change anything.
    """
    try: return _preview.build(args, resolve(args, ctx))
    except (ValueError, OSError): return None
