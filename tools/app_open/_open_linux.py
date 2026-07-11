import subprocess, time

def get_frontmost() -> str | None:
    r = subprocess.run(["xdotool", "getactivewindow"], capture_output=True, text=True)
    win_id = r.stdout.strip()
    return win_id or None

def open_app(app: str) -> str | None:
    try: subprocess.Popen([app])
    except Exception: subprocess.Popen(["xdg-open", app])

    deadline = time.time() + 5
    while time.time() < deadline:
        time.sleep(0.4)
        r = subprocess.run(["xdotool", "search", "--name", app], capture_output=True, text=True)
        ids = [i for i in r.stdout.strip().split("\n") if i]
        if ids: return ids[0]
    return None

def snap_region(win_id: str | None, x_start_frac: float, x_end_frac: float) -> bool:
    if not win_id: return False
    r = subprocess.run(["xdotool", "getdisplaygeometry"], capture_output=True, text=True)
    sw, sh = map(int, r.stdout.strip().split())
    x0, x1 = int(sw * x_start_frac), int(sw * x_end_frac)
    subprocess.run(["xdotool", "windowactivate", win_id], check=True)
    subprocess.run(["xdotool", "windowmove", win_id, str(x0), "0"], check=True)
    subprocess.run(["xdotool", "windowsize", win_id, str(x1 - x0), str(sh)], check=True)
    return True