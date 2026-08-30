import sys, platform
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))

from tools._pointer import verify

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
    # reports success otherwise. See tools/_pointer.py.
    out.update(verify(plat, x, y))
    return out