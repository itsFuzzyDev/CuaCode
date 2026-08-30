import sys, platform
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))

from tools._pointer import verify

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
    plat = _platform_module()
    plat.click(x, y, button, clicks)
    out = {"clicked_at": [x, y], "button": button, "clicks": clicks}
    # Where the pointer ended up, when the platform can tell us. A click that
    # went nowhere -- dropped for want of Accessibility permission, or aimed
    # off-screen and clamped -- otherwise reports success, and the agent spends
    # the next several rounds re-aiming at a target it can never hit.
    out.update(verify(plat, x, y))
    return out