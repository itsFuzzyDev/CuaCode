import sys, platform
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))

OS = platform.system()

def _platform_module():
    if OS == "Darwin": import _move_macos as m
    elif OS == "Windows": import _move_windows as m
    else: import _move_linux as m
    return m

def run(args: dict, ctx) -> dict:
    x, y = args["x"], args["y"]
    plat = _platform_module()
    plat.move(x, y)
    out = {"moved_to": [x, y]}
    # Same read-back the click tool does: a move that was silently dropped
    # reports success otherwise. See tools/click/main.py.
    landed = getattr(plat, "cursor", lambda: None)()
    if landed and (abs(landed[0] - x) > 1 or abs(landed[1] - y) > 1):
        out["landed_at"] = list(landed)
        out["warning"] = ("the pointer is not where it was sent; the event was probably dropped, "
                          "which on macOS means this app lacks Accessibility permission "
                          "(System Settings > Privacy & Security > Accessibility). Tell the user "
                          "rather than retrying the move")
    return out