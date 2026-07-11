def active_app_name() -> str:
    try:
        import win32gui, win32process, psutil
        hwnd = win32gui.GetForegroundWindow()
        _, pid = win32process.GetWindowThreadProcessId(hwnd)
        return psutil.Process(pid).name().replace(".exe", "")
    except Exception:
        return "unknown"

def capture():
    from PIL import Image
    import mss
    with mss.mss() as sct:
        shot = sct.grab(sct.monitors[1])
        return Image.frombytes("RGB", shot.size, shot.rgb)

def detect_scale(img_w: int) -> tuple[float, int]:
    try:
        import ctypes
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
        logical_w = ctypes.windll.user32.GetSystemMetrics(0)
        return img_w / logical_w if logical_w else 1.0, logical_w
    except Exception:
        return 1.0, img_w

FONT_PATH = "C:\\Windows\\Fonts\\segoeui.ttf"