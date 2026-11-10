import sys, io, base64, platform, threading
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))

from tools import _window
from handler import faults

from _grid import draw_grid
from _session import save_screenshot

OS = platform.system()

def brief(result: dict) -> dict:
    """One screenshot is one row; the encoded image lives in the message."""
    return {"result": {"n": 1}}

def _capture_module():
    if OS == "Darwin": import _capture_macos as m
    elif OS == "Windows": import _capture_windows as m
    else: import _capture_linux as m
    return m

def _archive(raw, gridded, session_dir):
    """Write both shots to the session, off the critical path.

    Encoding two full-size PNGs and asking the window server what app is in front costs
    more than the capture itself, and the agent is waiting on base64 that is already
    made. Not a daemon thread: the archive should survive a process that exits right
    after the capture.
    """
    def work():
        save_screenshot(raw, session_dir, OS, raw=True)
        save_screenshot(gridded, session_dir, OS, raw=False)
    threading.Thread(target=work).start()

def run(args: dict, ctx) -> dict:
    grid_size = max(2, int(args.get("grid_size") or 100))
    session_dir = getattr(ctx, "session_dir", None) if ctx else None
    self_name = getattr(ctx, "self_identity", None)

    # Park the terminal in the left strip, and on Windows bring it to the front
    # without giving it focus. Order matters: the terminal is raised first and the
    # windows being driven are raised after it, so the app the agent is looking at
    # ends up both in front and holding the keyboard, and the terminal sits behind
    # it on the left where it can still be read.
    raise_it = _window.OS == "Windows"
    self_snapped = False
    self_raised = False
    if self_name:
        try:
            self_snapped, self_raised = _window.park_self(self_name, raise_it=raise_it)
        except Exception as e:
            self_snapped = False
            self_raised = False
            faults.note("parking this worker's own window", e,
                        "the capture is taken with the window where it is")

    # Raising is a Windows need: input there follows the foreground, so the window being
    # driven has to hold it. On macOS activation decides the keyboard and can hop spaces,
    # and this runs on every capture. park_known only corrects drift, so it stays quiet.
    opened = []
    try:
        opened = _window.park_new(self_name, focus=raise_it)
        _window.park_known(self_name)
    except Exception as e:
        faults.note("parking the apps that appeared", e,
                    "a window opened during the capture stays where it landed")

    # The window in front is the one the agent is looking at, whether it was
    # opened by app_open or had already been running. Park it on the right even
    # when no baseline ever saw it appear, and keep it in front so the next
    # keystroke goes there. Without self_name the foreground could be the
    # terminal itself, so leave it alone rather than guessing.
    active_app = ""
    if self_name:
        try:
            active_app = _window.park_active(self_name, focus=raise_it)
        except Exception as e:
            active_app = ""
            faults.note("parking the window in front", e,
                        "the capture does not say which app is frontmost")

    plat = _capture_module()
    img = plat.capture()
    # Where the picture's top-left corner sits on the screen. Not always (0, 0): a
    # monitor can sit left of or above the primary one, and every label below is a
    # screen coordinate the click tools aim at.
    ox, oy = getattr(plat, "origin", lambda: (0, 0))()
    # Copied for the archive, because everything below mutates: draw_grid draws straight
    # onto whatever it is handed, and with no retina downscale to make a new image first
    # that would be this one, being written to disk on another thread.
    raw_copy = img.copy()

    # Down to logical points: the capture is framebuffer pixels, the click tools take
    # CGEvent points. A scale under 1 would mean the two readings disagree about which
    # screen this is, a bug rather than a no-op, so it is not shrunk.
    scale, logical_w, logical_h = plat.detect_scale(*img.size)
    if scale != 1.0:
        from PIL import Image
        img = img.resize((logical_w, logical_h), Image.LANCZOS)
    lw, lh = img.size

    region = args.get("region")
    origin, zoom, cw, ch = (ox, oy), 1.0, lw, lh
    if region:
        from PIL import Image
        # Clamped, because these come from the model: zoom 0 divides by zero
        # and zoom 1000 crops to a single point.
        zoom = min(32.0, max(1.0, float(args.get("zoom") or 2)))
        cx, cy = int(region[0]) - ox, int(region[1]) - oy
        cw, ch = max(1, min(lw, round(lw / zoom))), max(1, min(lh, round(lh / zoom)))
        x0 = max(0, min(cx - cw // 2, lw - cw))
        y0 = max(0, min(cy - ch // 2, lh - ch))
        # Enlarged by exactly `zoom`, not back to a fixed output size: the crop is rounded
        # points, and a stretch would drift the grid labels from what they point at.
        img = img.crop((x0, y0, x0 + cw, y0 + ch)).resize(
            (round(cw * zoom), round(ch * zoom)), Image.LANCZOS)
        origin = (x0 + ox, y0 + oy)
        if not args.get("grid_size"):
            grid_size = max(5, int(100 / zoom))

    img = draw_grid(img, grid_size, plat.FONT_PATH, origin=origin, scale=zoom)

    # PNG, because the grid is thin red lines and small text, exactly what JPEG smears.
    # compress_level=6 over optimize=True: identical bytes, 131ms cheaper per shot.
    buf = io.BytesIO()
    img.convert("RGB").save(buf, "PNG", compress_level=6)
    b64 = base64.b64encode(buf.getvalue()).decode()

    _archive(raw_copy, img, session_dir)

    out_meta = {"image_base64": b64, "width": lw, "height": lh, "grid_size": grid_size, "self_snapped": self_snapped}
    # Every monitor in the picture, so the model can tell where one ends and the next
    # begins rather than guessing from the labels alone.
    screens = getattr(plat, "monitors", lambda: [])()
    if screens: out_meta["monitors"] = screens
    if self_raised: out_meta["self_raised"] = True
    if opened: out_meta["opened_apps"] = opened
    if active_app: out_meta["parked_active_app"] = active_app
    if region:
        # Straight from the crop box, not recomputed from the zoom: the two roundings
        # disagreed, and this is the number the agent uses to tell whether the point it
        # wants is even inside the frame.
        out_meta["zoomed_region"] = {"origin": list(origin), "zoom": zoom,
                                     "covers": [origin[0], origin[1], origin[0] + cw, origin[1] + ch]}
    return out_meta
