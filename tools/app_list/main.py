import sys, platform
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))

OS = platform.system()

def _platform_module():
    if OS == "Darwin": import _list_macos as m
    elif OS == "Windows": import _list_windows as m
    else: import _list_linux as m
    return m

def run(args: dict, ctx) -> dict:
    m = _platform_module()
    return {"running": m.running(), "installed": m.installed()}