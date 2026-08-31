import sys, io, base64, platform, threading
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))

from tools import _window

from _grid import draw_grid
from _session import save_screenshot

OS = platform.system()

def _capture_module():
    if OS == "Darwin": import _capture_macos as m
    elif OS == "Windows": import _capture_windows as m
    else: import _capture_linux as m
    return m

def _archive(raw, gridded, session_dir):
    """Write both shots to the session, off the critical path.

    Encoding two full-size PNGs and asking the window server what app is in
    front costs more than the capture itself, and none of it is on the path to
    the answer -- the agent is waiting on the base64, which is already made.
    Not a daemon thread: the archive should survive a process that exits right
    after the capture, and the wait at shutdown is one PNG encode.
    """
    def work():
        save_screenshot(raw, session_dir, OS, raw=True)
        save_screenshot(gridded, session_dir, OS, raw=False)
    threading.Thread(target=work).start()

def run(args: dict, ctx) -> dict:
    grid_size = max(2, int(args.get("grid_size") or 100))
    session_dir = getattr(ctx, "session_dir", None) if ctx else None
    self_name = getattr(ctx, "self_identity", None)

    # Park our terminal in the left strip, without raising it, on every capture:
    # nothing else re-checks parking, and raising here would leave the terminal
    # focused and eat the agent's next click. A window-manager failure never
    # propagates -- losing the shot is worse than a bad parking job.
    self_snapped = False
    if self_name:
        try:
            self_snapped = _window.park_self(self_name)
        except Exception:
            self_snapped = False

    # Same guarantee for apps the agent just opened: a process shows up in the
    # app list before it has a window, so parking is redone here on the next
    # call, and already-parked windows drift back into place.
    opened = []
    try:
        opened = _window.park_new(self_name, focus=False)
        _window.park_known(self_name)
    except Exception:
        pass

    plat = _capture_module()
    img = plat.capture()
    # Copied for the archive, because everything below mutates: draw_grid draws
    # straight onto whatever it is handed, and with no retina downscale to make
    # a new image first, that would be this one -- being written to disk on
    # another thread at the same time.
    raw_copy = img.copy()

    # Down to logical points: the capture is framebuffer pixels, the click tools
    # take CGEvent points. A scale under 1 would mean the two readings disagree
    # about which screen this is -- a bug, not a no-op, so it is not shrunk.
    scale, logical_w, logical_h = plat.detect_scale(*img.size)
    if scale != 1.0:
        from PIL import Image
        img = img.resize((logical_w, logical_h), Image.LANCZOS)
    lw, lh = img.size

    region = args.get("region")
    origin, zoom, cw, ch = (0, 0), 1.0, lw, lh
    if region:
        from PIL import Image
        # Clamped, because these come from the model: zoom 0 divides by zero
        # and zoom 1000 crops to a single point.
        zoom = min(32.0, max(1.0, float(args.get("zoom") or 2)))
        cx, cy = int(region[0]), int(region[1])
        cw, ch = max(1, min(lw, round(lw / zoom))), max(1, min(lh, round(lh / zoom)))
        x0 = max(0, min(cx - cw // 2, lw - cw))
        y0 = max(0, min(cy - ch // 2, lh - ch))
        # Enlarged by exactly `zoom`, not back to a fixed output size: the crop
        # is rounded points, and a stretch would drift the grid labels from what
        # they point at by the far edge.
        img = img.crop((x0, y0, x0 + cw, y0 + ch)).resize(
            (round(cw * zoom), round(ch * zoom)), Image.LANCZOS)
        origin = (x0, y0)
        if not args.get("grid_size"):
            grid_size = max(5, int(100 / zoom))

    img = draw_grid(img, grid_size, plat.FONT_PATH, origin=origin, scale=zoom)

    # PNG, labelled PNG -- the grid is thin red lines and small text, exactly
    # what JPEG smears. compress_level=6 over optimize=True: identical bytes,
    # 131ms cheaper per shot.
    buf = io.BytesIO()
    img.convert("RGB").save(buf, "PNG", compress_level=6)
    b64 = base64.b64encode(buf.getvalue()).decode()

    _archive(raw_copy, img, session_dir)

    out_meta = {"image_base64": b64, "width": lw, "height": lh, "grid_size": grid_size, "self_snapped": self_snapped}
    if opened: out_meta["opened_apps"] = opened
    if region:
        # Straight from the crop box, not recomputed from the zoom: the two
        # roundings disagreed, and this is the number the agent uses to tell
        # whether the point it wants is even inside the frame.
        out_meta["zoomed_region"] = {"origin": list(origin), "zoom": zoom,
                                     "covers": [origin[0], origin[1], origin[0] + cw, origin[1] + ch]}
    return out_meta
