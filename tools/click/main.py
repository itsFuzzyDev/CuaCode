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
    plat = _platform_module()
    plat.click(x, y, button, clicks)
    out = {"clicked_at": [x, y], "button": button, "clicks": clicks}
    # Where the pointer ended up, when the platform can tell us. A click that
    # went nowhere -- dropped for want of Accessibility permission, or aimed
    # off-screen and clamped -- otherwise reports success, and the agent spends
    # the next several rounds re-aiming at a target it can never hit.
    landed = getattr(plat, "cursor", lambda: None)()
    if landed and (abs(landed[0] - x) > 1 or abs(landed[1] - y) > 1):
        out["landed_at"] = list(landed)
        out["warning"] = ("the pointer is not where the click was aimed; the event was probably "
                          "dropped, which on macOS means this app lacks Accessibility permission "
                          "(System Settings > Privacy & Security > Accessibility). Tell the user "
                          "rather than retrying the click")
    return out