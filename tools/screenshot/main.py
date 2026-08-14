import sys, io, base64, platform, importlib.util, threading
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))

from _grid import draw_grid
from _session import save_screenshot

OS = platform.system()

_SIBLINGS = {}

def _load_sibling_tool_module(tool_folder: str, filename: str):
    """A tool module from another folder, loaded once per process.

    Cached because this runs on the capture path: without it every screenshot
    re-read and re-executed the file, which meant the window module's own
    caches -- the Accessibility handles, the record of where a window was last
    verified to be parked -- were thrown away between one capture and the next,
    and the expensive first call was the only call there ever was.
    """
    key = (tool_folder, filename)
    if key not in _SIBLINGS:
        path = Path(__file__).parent.parent / tool_folder / filename
        spec = importlib.util.spec_from_file_location(f"tools.{tool_folder}.{filename}", path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        _SIBLINGS[key] = mod
    return _SIBLINGS[key]

def _snap_module():
    if OS == "Darwin": return _load_sibling_tool_module("app_open", "_open_macos.py")
    elif OS == "Windows": return _load_sibling_tool_module("app_open", "_open_windows.py")
    else: return _load_sibling_tool_module("app_open", "_open_linux.py")

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

    # Park our own terminal in the left strip so it never covers the app being
    # driven -- but without raising it. This runs on every capture, and it is
    # the last window action before the shot, so raising here would leave the
    # terminal focused and send the agent's next click/keystroke to itself.
    #
    # It runs on every capture on purpose: nothing else re-checks. The agent
    # will not think to call app_open again when a window has drifted back over
    # the terminal, so the guarantee has to be re-established here or not at
    # all. What it costs is a single geometry read in the case that matters --
    # the window is still parked -- and only the drifted case pays for a move.
    #
    # Parking is cosmetic; the capture is the tool. A window-manager call that
    # fails must not cost the agent its only view of the screen, so nothing
    # here is allowed to propagate.
    self_snapped = False
    if self_name:
        try:
            self_snapped = _snap_module().snap_region(self_name, 0.0, 0.3, focus=False)
        except Exception:
            self_snapped = False

    plat = _capture_module()
    img = plat.capture()
    # Copied for the archive, because everything below mutates: draw_grid draws
    # straight onto whatever it is handed, and with no retina downscale to make
    # a new image first, that would be this one -- being written to disk on
    # another thread at the same time.
    raw_copy = img.copy()

    # Down to logical points before anything is drawn or reported. The grid the
    # agent reads coordinates off has to be in the unit the click tools take,
    # and on a retina display the capture is not: it comes back in framebuffer
    # pixels, twice the points CGEvent moves the cursor in. The resize is not
    # guarded on scale > 1: a ratio under 1 means the two readings disagree
    # about which screen was captured, which is a bug, not a no-op.
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
        # Enlarged by exactly `zoom`, rather than back up to the full screen
        # size. The crop is a rounded number of points, so stretching it to a
        # fixed output would make the real magnification a hair off the number
        # the grid is labelled with, and the labels drift a pixel or two from
        # what they point at by the far edge -- worst at high zoom, which is
        # the case the zoom exists for.
        img = img.crop((x0, y0, x0 + cw, y0 + ch)).resize(
            (round(cw * zoom), round(ch * zoom)), Image.LANCZOS)
        origin = (x0, y0)
        if not args.get("grid_size"):
            grid_size = max(5, int(100 / zoom))

    img = draw_grid(img, grid_size, plat.FONT_PATH, origin=origin, scale=zoom)

    # PNG, and named as such. The bytes were always PNG -- the .jpg temp file
    # and the quality= that PNG ignores were both decoration -- but the label
    # is what the providers key their media type off. Lossy encoding is the
    # wrong trade here anyway: the grid is thin red lines and small text over
    # UI, which is exactly what a JPEG smears.
    #
    # compress_level=6 rather than optimize=True: optimize re-runs the encoder
    # over several filter strategies and keeps the smallest, which on a screen
    # full of flat UI costs 131ms to save 9KB out of 338. The bytes are
    # identical either way -- PNG is lossless at every level -- so the only
    # thing being traded is a rounding error of upload against a fifth of a
    # second of the agent sitting still.
    buf = io.BytesIO()
    img.convert("RGB").save(buf, "PNG", compress_level=6)
    b64 = base64.b64encode(buf.getvalue()).decode()

    _archive(raw_copy, img, session_dir)

    out_meta = {"image_base64": b64, "width": lw, "height": lh, "grid_size": grid_size, "self_snapped": self_snapped}
    if region:
        # Straight from the crop box, not recomputed from the zoom: the two
        # roundings disagreed, and this is the number the agent uses to tell
        # whether the point it wants is even inside the frame.
        out_meta["zoomed_region"] = {"origin": list(origin), "zoom": zoom,
                                     "covers": [origin[0], origin[1], origin[0] + cw, origin[1] + ch]}
    return out_meta
