import sys, os, tempfile, base64, platform, importlib.util
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))

from _grid import draw_grid
from _session import save_screenshot

OS = platform.system()

def _load_sibling_tool_module(tool_folder: str, filename: str):
    path = Path(__file__).parent.parent / tool_folder / filename
    spec = importlib.util.spec_from_file_location(f"tools.{tool_folder}.{filename}", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod

def _snap_module():
    if OS == "Darwin": return _load_sibling_tool_module("app_open", "_open_macos.py")
    elif OS == "Windows": return _load_sibling_tool_module("app_open", "_open_windows.py")
    else: return _load_sibling_tool_module("app_open", "_open_linux.py")

def _capture_module():
    if OS == "Darwin": import _capture_macos as m
    elif OS == "Windows": import _capture_windows as m
    else: import _capture_linux as m
    return m

def run(args: dict, ctx) -> dict:
    grid_size = args.get("grid_size", 100)
    session_dir = getattr(ctx, "session_dir", None) if ctx else None
    self_name = getattr(ctx, "self_identity", None)

    snap = _snap_module()
    self_snapped = snap.snap_region(self_name, 0.0, 0.3) if self_name else False

    plat = _capture_module()
    img = plat.capture()
    save_screenshot(img, session_dir, OS, raw=True)
    w, _ = img.size

    scale, logical_w = plat.detect_scale(w)
    if scale > 1.0:
        from PIL import Image
        logical_h = int(img.size[1] / scale)
        img = img.resize((logical_w, logical_h), Image.LANCZOS)
    lw, lh = img.size

    region = args.get("region")
    origin, zoom = (0, 0), 1.0
    if region:
        from PIL import Image
        zoom = float(args.get("zoom", 2))
        cx, cy = int(region[0]), int(region[1])
        cw, ch = int(lw / zoom), int(lh / zoom)
        x0 = max(0, min(cx - cw // 2, lw - cw))
        y0 = max(0, min(cy - ch // 2, lh - ch))
        img = img.crop((x0, y0, x0 + cw, y0 + ch)).resize((lw, lh), Image.LANCZOS)
        origin = (x0, y0)
        grid_size = args.get("grid_size", max(20, int(100 / zoom)))

    img = draw_grid(img, grid_size, plat.FONT_PATH, origin=origin, scale=zoom)
    save_screenshot(img, session_dir, OS, raw=False)

    with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as f: out = f.name
    try:
        img.convert("RGB").save(out, "JPEG", quality=86, optimize=True)
        b64 = base64.b64encode(open(out, "rb").read()).decode()
    finally:
        os.unlink(out)

    out_meta = {"image_base64": b64, "width": lw, "height": lh, "grid_size": grid_size, "self_snapped": self_snapped}
    if region:
        out_meta["zoomed_region"] = {"origin": list(origin), "zoom": zoom,
                                     "covers": [origin[0], origin[1], origin[0] + int(lw / zoom), origin[1] + int(lh / zoom)]}
    return out_meta