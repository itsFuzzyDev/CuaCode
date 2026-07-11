import subprocess, os

def running() -> list[str]:
    r = subprocess.run(["osascript", "-e",
        'tell application "System Events" to get name of every application process whose visible is true'],
        capture_output=True, text=True)
    return [a.strip() for a in r.stdout.strip().split(",") if a.strip()]

def installed() -> list[str]:
    apps = []
    for d in ("/Applications", "/System/Applications", os.path.expanduser("~/Applications")):
        try:
            for f in os.listdir(d):
                if f.endswith(".app"): apps.append(f[:-4])
        except Exception:
            pass
    return sorted(set(apps))