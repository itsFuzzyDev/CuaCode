import sys, platform
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))

OS = platform.system()

def _platform_module():
    if OS == "Darwin": import _key_macos as m
    elif OS == "Windows": import _key_windows as m
    else: import _key_linux as m
    return m

def run(args: dict, ctx) -> dict:
    combo = args["combo"]
    _platform_module().press(combo)
    return {"pressed": combo}