"""The whole desktop as one image, in true screen coordinates.

Every monitor goes in, not just the primary, and the image starts at the top-left of
the virtual desktop: origin() says where that is, so a coordinate read off the picture
is the coordinate the click tools take.

mss reads the pixel grid directly but cannot under Wayland, where the X root window it
reads has no content and every grab comes back black. There the desktop is asked for
its own picture through the screenshot portal, which returns the same thing: one image
covering every monitor.
"""

import os, subprocess, time
from pathlib import Path

from tools._display import cursor, env

_ORIGIN = (0, 0)
_MONITORS = []

def _mss():
    import mss
    return mss.mss()

def monitors() -> list[dict]:
    """Every monitor's rectangle in screen coordinates, primary flagged. Empty if unknown."""
    try:
        with _mss() as sct:
            return [{"x": m["left"], "y": m["top"], "width": m["width"], "height": m["height"],
                     "primary": bool(m.get("is_primary")), "name": m.get("output", "")}
                    for m in sct.monitors[1:]]
    except Exception:
        return []

def _virtual() -> tuple[int, int, int, int]:
    """Bounding box of every monitor: (left, top, width, height), zero when unknown.

    Laid out left to right and above below, this is what a capture of the whole
    desktop has to be, and what its top-left corner is on the screen.
    """
    if not _MONITORS: return 0, 0, 0, 0
    x0 = min(m["x"] for m in _MONITORS)
    y0 = min(m["y"] for m in _MONITORS)
    x1 = max(m["x"] + m["width"] for m in _MONITORS)
    y1 = max(m["y"] + m["height"] for m in _MONITORS)
    return x0, y0, x1 - x0, y1 - y0

def origin() -> tuple[int, int]:
    """Where the last capture's top-left corner sits on the screen."""
    return _ORIGIN

def _blank(img) -> bool:
    return not any(hi for _, hi in img.convert("RGB").getextrema())

def _x11() -> object:
    try:
        from PIL import Image
        with _mss() as sct:
            shot = sct.grab(sct.monitors[0])   # [0] is every monitor, [1] only the primary
            return Image.frombytes("RGB", shot.size, shot.rgb)
    except Exception:
        return None

def _pictures() -> Path:
    try:
        out = subprocess.run(["xdg-user-dir", "PICTURES"], capture_output=True, text=True,
                             env=env(), timeout=5).stdout.strip()
        if out and os.path.isdir(out): return Path(out)
    except Exception:
        pass
    return Path.home() / "Pictures"

def _wait_newest(folder: Path, before: set[str], timeout: float = 6.0) -> Path | None:
    """The portal writes its own file and says nothing useful until it is done growing."""
    end = time.time() + timeout
    seen, size = None, -1
    while time.time() < end:
        new = [p for p in folder.glob("Screenshot*.png") if p.name not in before]
        if new:
            p = max(new, key=lambda q: q.stat().st_mtime)
            if p == seen and p.stat().st_size == size and size > 0: return p
            seen, size = p, p.stat().st_size
        time.sleep(0.1)
    return None

def _portal():
    """Ask the desktop for its own picture. None when the portal will not answer."""
    e = env()
    if not e.get("DBUS_SESSION_BUS_ADDRESS"): return None
    folder = _pictures()
    before = {p.name for p in folder.glob("Screenshot*.png")}
    try:
        subprocess.run(
            ["busctl", "--user", "call", "org.freedesktop.portal.Desktop",
             "/org/freedesktop/portal/desktop", "org.freedesktop.portal.Screenshot",
             "Screenshot", "sa{sv}", "", "0"],
            env=e, check=True, capture_output=True, timeout=10)
    except Exception:
        return None
    path = _wait_newest(folder, before)
    if path is None: return None
    try:
        from PIL import Image
        with Image.open(path) as im:
            img = im.convert("RGB").copy()
    except Exception:
        return None
    try: path.unlink()   # ours, and one file per capture would litter
    except OSError: pass
    return img

def capture():
    global _ORIGIN, _MONITORS
    env()          # before mss: it reads the process environment, not the dict
    _MONITORS = monitors()
    x0, y0, _, _ = _virtual()
    _ORIGIN = (x0, y0)
    img = _x11()
    if img is not None and not _blank(img):
        return img
    img = _portal()
    if img is not None:
        return img
    # Nothing better to say than what mss did give: black, but the right size, so the
    # grid and its labels still line up with the screen.
    return img if img is not None else _x11()

def detect_scale(img_w: int, img_h: int) -> tuple[float, int, int]:
    # A HiDPI desktop is captured at framebuffer pixels while the click tools take
    # logical ones, so the ratio to the monitors' own size is the scale between them.
    _, _, w, h = _virtual()
    if w and h and img_w > w * 1.01:
        s = img_w / w
        if s <= 4: return s, w, h
    return 1.0, img_w, img_h

def active_app_name() -> str:
    try:
        r = subprocess.run(["xdotool", "getactivewindow", "getwindowname"],
                            capture_output=True, text=True, env=env(), timeout=5)
        return r.stdout.strip().replace(" ", "_")[:40] or "unknown"
    except Exception:
        return "unknown"

FONT_PATH = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
