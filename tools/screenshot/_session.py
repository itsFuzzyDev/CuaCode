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

def save_screenshot(img, session_dir, os_name: str, raw: bool) -> str | None:
    # ctx carries the session directory as a string, not a Path -- it crosses
    # the IPC boundary as JSON. Take either.
    if not session_dir: return None
    # Archiving to the session is bookkeeping; the capture is the tool. An
    # unwritable directory must not cost the agent its view of the screen.
    try:
        shots_dir = Path(session_dir) / "screenshots"
        shots_dir.mkdir(exist_ok=True, parents=True)
        # Milliseconds, not seconds. Two captures inside the same second wrote
        # the same filename and the second silently replaced the first, which
        # an agent taking a burst of shots does constantly -- and now that the
        # writes happen on a background thread, two of them can be in flight at
        # once.
        dest = shots_dir / f"{active_app_name(os_name)}_{int(time.time() * 1000)}{'_raw' if raw else ''}.png"
        img.save(dest, "PNG")
        return str(dest)
    except Exception:
        return None