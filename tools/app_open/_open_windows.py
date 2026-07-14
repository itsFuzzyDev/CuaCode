import subprocess, time, ctypes, win32gui, win32process, win32con, psutil

def get_frontmost() -> int | None:
    hwnd = win32gui.GetForegroundWindow()
    return hwnd or None

def open_app(app: str) -> int | None:
    try:
        subprocess.Popen(app)
    except Exception:
        try: subprocess.Popen(["cmd", "/c", "start", "", app], shell=False)
        except Exception: return None

    deadline = time.time() + 5
    target = app.lower()
    while time.time() < deadline:
        time.sleep(0.4)
        found = []
        def cb(hwnd, _):
            if not win32gui.IsWindowVisible(hwnd): return
            _, pid = win32process.GetWindowThreadProcessId(hwnd)
            try: name = psutil.Process(pid).name().lower()
            except Exception: return
            if target in name or name.replace(".exe", "") in target: found.append(hwnd)
        win32gui.EnumWindows(cb, None)
        if found: return found[0]
    return None

def snap_region(hwnd_or_name: int | str | None, x_start_frac: float, x_end_frac: float) -> bool:
    if not hwnd_or_name: return False
    hwnd = hwnd_or_name
    if isinstance(hwnd_or_name, str):
        found = []
        target = hwnd_or_name.lower()
        def cb(hwnd, _):
            if not win32gui.IsWindowVisible(hwnd): return
            _, pid = win32process.GetWindowThreadProcessId(hwnd)
            try: name = psutil.Process(pid).name().lower()
            except Exception: return
            if target in name or name.replace(".exe", "") in target: found.append(hwnd)
        win32gui.EnumWindows(cb, None)
        if not found: return False
        hwnd = found[0]
    sw = ctypes.windll.user32.GetSystemMetrics(0)
    sh = ctypes.windll.user32.GetSystemMetrics(1)
    x0, x1 = int(sw * x_start_frac), int(sw * x_end_frac)
    win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
    win32gui.SetForegroundWindow(hwnd)
    win32gui.MoveWindow(hwnd, x0, 0, x1 - x0, sh, True)
    return True