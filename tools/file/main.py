import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))

import _io_ops
import _search
from _common import resolve

ACTIONS = {
    "read": _io_ops.read,
    "write": _io_ops.write,
    "edit": _io_ops.edit,
    "mkdir": _io_ops.mkdir,
    "glob": _search.glob,
    "grep": _search.grep,
}

def run(args: dict, ctx) -> dict:
    handler = ACTIONS.get(args["action"])
    if not handler: return {"error": f"unknown action: {args['action']}"}
    return handler(resolve(args), args)
