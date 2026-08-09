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

def snap_region(hwnd_or_name: int | str | None, x_start_frac: float, x_end_frac: float, focus: bool = False) -> bool:
    """See _open_macos.snap_region -- focus defaults to False so repositioning
    a window does not decide what has keyboard focus."""
    if not hwnd_or_name: return False
    hwnd = hwnd_or_name
    if isinstance(hwnd_or_name, str):
        found = []
        # The frontend probes this name with PowerShell, which can hand back
        # several lines when more than one process owns the foreground window.
        target = hwnd_or_name.strip().splitlines()[0].strip().lower()
        if not target: return False
        def cb(hwnd, _):
            if not win32gui.IsWindowVisible(hwnd): return
            _, pid = win32process.GetWindowThreadProcessId(hwnd)
            try: name = psutil.Process(pid).name().lower()
            except Exception: return
            if target in name or name.replace(".exe", "") in target: found.append(hwnd)
        win32gui.EnumWindows(cb, None)
        if not found: return False
        hwnd = found[0]
    # pywin32 wants a plain int for an HWND. Anything else -- a PyHANDLE, a
    # float off the wire -- raises "The object is not a PyHANDLE object" from
    # inside ShowWindow/MoveWindow, which used to take the whole screenshot
    # down with it. Coerce here, and treat a bad handle as "not snapped".
    try: hwnd = int(hwnd)
    except (TypeError, ValueError): return False
    if not hwnd or not win32gui.IsWindow(hwnd): return False
    sw = ctypes.windll.user32.GetSystemMetrics(0)
    sh = ctypes.windll.user32.GetSystemMetrics(1)
    x0, x1 = int(sw * x_start_frac), int(sw * x_end_frac)
    # SW_RESTORE also activates; SW_SHOWNOACTIVATE un-minimizes without stealing focus.
    win32gui.ShowWindow(hwnd, win32con.SW_RESTORE if focus else win32con.SW_SHOWNOACTIVATE)
    if focus: win32gui.SetForegroundWindow(hwnd)
    win32gui.MoveWindow(hwnd, x0, 0, x1 - x0, sh, True)
    return True