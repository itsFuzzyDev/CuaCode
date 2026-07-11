import subprocess, time

def get_frontmost() -> str | None:
    try:
        r = subprocess.run(["osascript", "-e",
            'tell application "System Events" to get name of first process whose frontmost is true'],
            capture_output=True, text=True)
        return r.stdout.strip() or None
    except Exception:
        return None

def _wait_for_window(proc_name: str, timeout: float = 6) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        r = subprocess.run(["osascript", "-e",
            f'tell application "System Events" to get count of windows of process "{proc_name}"'],
            capture_output=True, text=True)
        try:
            if int(r.stdout.strip()) > 0: return True
        except ValueError:
            pass
        time.sleep(0.3)
    return False

def open_app(app: str) -> str | None:
    try:
        subprocess.run(["open", "-a", app], check=True)
    except subprocess.CalledProcessError:
        subprocess.run(["open", app], check=True)

    deadline = time.time() + 12
    proc_name = None
    while time.time() < deadline:
        time.sleep(0.4)
        r = subprocess.run(["osascript", "-e",
            'tell application "System Events" to get name of every application process whose visible is true'],
            capture_output=True, text=True)
        names = [n.strip() for n in r.stdout.strip().split(",") if n.strip()]
        for n in names:
            if n.lower() == app.lower():
                proc_name = n
                break
        if proc_name: break

    if proc_name and _wait_for_window(proc_name):
        return proc_name
    return None

def snap_region(proc_name: str | None, x_start_frac: float, x_end_frac: float) -> bool:
    if not proc_name: return False
    try:
        r = subprocess.run(["osascript", "-e", 'tell application "Finder" to get bounds of window of desktop'],
                            capture_output=True, text=True)
        _, _, sw, sh = [int(v.strip()) for v in r.stdout.strip().split(",")]
        x0, x1 = int(sw * x_start_frac), int(sw * x_end_frac)
        script = f'''tell application "System Events"
            tell process "{proc_name}"
                set frontmost to true
                set position of window 1 to {{{x0}, 0}}
                set size of window 1 to {{{x1 - x0}, {sh}}}
            end tell
        end tell'''
        r2 = subprocess.run(["osascript", "-e", script], capture_output=True, text=True)
        return r2.returncode == 0
    except Exception:
        return False