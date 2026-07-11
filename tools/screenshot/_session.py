import time
from pathlib import Path

def active_app_name(os_name: str) -> str:
    if os_name == "Darwin":
        from _capture_macos import active_app_name as f
    elif os_name == "Windows":
        from _capture_windows import active_app_name as f
    else:
        from _capture_linux import active_app_name as f
    return f()

def save_screenshot(img, session_dir: Path, os_name: str, raw: bool) -> str | None:
    if session_dir is None: return None
    shots_dir = session_dir / "screenshots"
    shots_dir.mkdir(exist_ok=True, parents=True)
    dest = shots_dir / f"{active_app_name(os_name)}_{int(time.time())}{'_raw' if raw else ''}.png"
    img.save(dest, "PNG")
    return str(dest)