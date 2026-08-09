import winreg, win32gui, win32process, psutil

def running() -> list[str]:
    names = set()
    def cb(hwnd, _):
        if not win32gui.IsWindowVisible(hwnd) or not win32gui.GetWindowText(hwnd): return
        _, pid = win32process.GetWindowThreadProcessId(hwnd)
        try: names.add(psutil.Process(pid).name().replace(".exe", ""))
        except Exception: pass
    win32gui.EnumWindows(cb, None)
    return sorted(names)

def installed() -> list[str]:
    apps = []
    roots = [
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall"),
        (winreg.HKEY_CURRENT_USER, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
    ]
    for hive, path in roots:
        try:
            key = winreg.OpenKey(hive, path)
            for i in range(winreg.QueryInfoKey(key)[0]):
                sub = winreg.EnumKey(key, i)
                try:
                    subkey = winreg.OpenKey(key, sub)
                    name, _ = winreg.QueryValueEx(subkey, "DisplayName")
                    if name: apps.append(name)
                except Exception:
                    continue
        except Exception:
            continue
    return sorted(set(apps))