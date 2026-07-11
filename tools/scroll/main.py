import sys, platform
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))

OS = platform.system()

def _platform_module():
    if OS == "Darwin": import _scroll_macos as m
    elif OS == "Windows": import _scroll_windows as m
    else: import _scroll_linux as m
    return m

def run(args: dict, ctx) -> dict:
    x, y = args["x"], args["y"]
    dx, dy = args.get("dx", 0), args.get("dy", 0)
    _platform_module().scroll(x, y, dx, dy)
    return {"scrolled_at": [x, y], "dx": dx, "dy": dy}