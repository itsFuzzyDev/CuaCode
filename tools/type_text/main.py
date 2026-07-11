import sys, platform
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))

OS = platform.system()

def _platform_module():
    if OS == "Darwin": import _type_macos as m
    elif OS == "Windows": import _type_windows as m
    else: import _type_linux as m
    return m

def run(args: dict, ctx) -> dict:
    text = args["text"]
    delay = args.get("delay", 0.0001)
    _platform_module().type_text(text, delay)
    return {"typed": text}