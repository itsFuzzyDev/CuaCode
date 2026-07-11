import sys, platform
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))

OS = platform.system()

def _platform_module():
    if OS == "Darwin": import _open_macos as m
    elif OS == "Windows": import _open_windows as m
    else: import _open_linux as m
    return m

def run(args: dict, ctx) -> dict:
    app = args["app"]
    m = _platform_module()

    self_handle = m.get_frontmost()
    self_snapped = m.snap_region(self_handle, 0.0, 0.3)

    handle = m.open_app(app)
    if not handle:
        return {"ok": True, "app": app, "snapped": False, "self_snapped": self_snapped,
                "note": "app opened but window not detected within 5s"}

    app_snapped = m.snap_region(handle, 0.3, 1.0)
    return {"ok": True, "app": app, "snapped": app_snapped, "self_snapped": self_snapped}