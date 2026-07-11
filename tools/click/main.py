import sys, platform
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))

OS = platform.system()

def _platform_module():
    if OS == "Darwin": import _click_macos as m
    elif OS == "Windows": import _click_windows as m
    else: import _click_linux as m
    return m

def run(args: dict, ctx) -> dict:
    x, y = args["x"], args["y"]
    button = args.get("button", "left")
    clicks = args.get("clicks", 1)
    _platform_module().click(x, y, button, clicks)
    return {"clicked_at": [x, y], "button": button, "clicks": clicks}