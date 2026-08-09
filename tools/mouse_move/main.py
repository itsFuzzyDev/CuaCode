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
    _platform_module().move(x, y)
    return {"moved_to": [x, y]}