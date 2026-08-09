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

def detect_scale(img_w: int) -> tuple[float, int]:
    try:
        import ctypes
        logical_w = ctypes.windll.user32.GetSystemMetrics(0)
        return img_w / logical_w if logical_w else 1.0, logical_w
    except Exception:
        return 1.0, img_w

FONT_PATH = "C:\\Windows\\Fonts\\segoeui.ttf"