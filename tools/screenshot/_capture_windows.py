def active_app_name() -> str:
    try:
        import win32gui, win32process, psutil
        hwnd = win32gui.GetForegroundWindow()
        _, pid = win32process.GetWindowThreadProcessId(hwnd)
        return psutil.Process(pid).name().replace(".exe", "")
    except Exception:
        return "unknown"

_dpi_done = False

def _dpi_aware():
    """DPI awareness is process-wide and one-shot -- it has to be set before the
    first capture, not after. Set late, one shot came back in logical pixels and
    the next in physical, and the grid the agent clicks from silently changed
    units mid-session."""
    global _dpi_done
    if _dpi_done: return
    _dpi_done = True
    import ctypes
    for call in (lambda: ctypes.windll.user32.SetProcessDpiAwarenessContext(-4),  # per-monitor v2
                 lambda: ctypes.windll.shcore.SetProcessDpiAwareness(2),
                 lambda: ctypes.windll.user32.SetProcessDPIAware()):
        try:
            if call(): return
        except Exception:
            continue

def capture():
    from PIL import Image
    import mss
    _dpi_aware()
    with mss.mss() as sct:
        shot = sct.grab(sct.monitors[1])
        return Image.frombytes("RGB", shot.size, shot.rgb)

def detect_scale(img_w: int, img_h: int) -> tuple[float, int, int]:
    """Primary monitor size in the units this process sees.

    Both readings are taken after _dpi_aware(), so a DPI-aware process gets
    physical pixels from GetSystemMetrics and from mss alike -- scale 1.0, and
    SetCursorPos speaks the same unit. The ratio is still computed rather than
    assumed, because a failed awareness call flips both to virtualized pixels.
    Errors are not swallowed: a wrong scale mislabels the grid silently.
    """
    import ctypes
    u = ctypes.windll.user32
    logical_w, logical_h = int(u.GetSystemMetrics(0)), int(u.GetSystemMetrics(1))
    if not logical_w or not logical_h:
        raise RuntimeError("primary monitor reported a zero size; cannot map screenshot pixels to click coordinates")
    sx, sy = img_w / logical_w, img_h / logical_h
    if abs(sx - sy) > 0.01:
        raise RuntimeError(
            f"screenshot is {img_w}x{img_h} against a {logical_w}x{logical_h} primary monitor, "
            f"which is not a uniform scale ({sx:.3f} x {sy:.3f}); refusing to guess, "
            "because clicks would land off target")
    return sx, logical_w, logical_h

FONT_PATH = "C:\\Windows\\Fonts\\segoeui.ttf"