import subprocess, os, glob, configparser

def running() -> list[str]:
    r = subprocess.run(["xdotool", "search", "--name", ""], capture_output=True, text=True)
    names = set()
    for win_id in r.stdout.strip().split("\n"):
        if not win_id: continue
        n = subprocess.run(["xdotool", "getwindowname", win_id], capture_output=True, text=True)
        if n.stdout.strip(): names.add(n.stdout.strip())
    return sorted(names)

def installed() -> list[str]:
    apps = []
    for d in ("/usr/share/applications", os.path.expanduser("~/.local/share/applications")):
        for f in glob.glob(os.path.join(d, "*.desktop")):
            try:
                cfg = configparser.ConfigParser(interpolation=None)
                cfg.read(f, encoding="utf-8")
                name = cfg.get("Desktop Entry", "Name", fallback=None)
                if name: apps.append(name)
            except Exception:
                continue
    return sorted(set(apps))