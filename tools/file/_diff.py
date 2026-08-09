import difflib

from _common import DIFF_CAP

def unified(before: str, after: str, name: str) -> str:
    """What actually changed, handed back with the result.

    An edit that matched the wrong hunk, or landed at the wrong indentation, looks
    exactly like a successful one in an {"ok": true} -- and is usually found three
    calls later. Echoing the patch means the model reviews its own edit in the same
    turn it made it, and the frontend gets something to render for free.
    """
    lines = list(difflib.unified_diff(before.splitlines(), after.splitlines(),
                                      fromfile=f"a/{name}", tofile=f"b/{name}",
                                      lineterm="", n=3))
    if len(lines) > DIFF_CAP:
        lines = lines[:DIFF_CAP] + [f"... {len(lines) - DIFF_CAP} more diff lines"]
    return "\n".join(lines)
