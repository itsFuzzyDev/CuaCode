import subprocess, tempfile, os

def active_app_name() -> str:
    try:
        r = subprocess.run(["osascript", "-e",
            'tell application "System Events" to get name of first process whose frontmost is true'],
            capture_output=True, text=True)
        return r.stdout.strip().replace(" ", "_") or "unknown"
    except Exception:
        return "unknown"

def capture():
    from PIL import Image
    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f: tmp = f.name
    try:
        subprocess.run(["screencapture", "-x", tmp], check=True)
        return Image.open(tmp)
    finally:
        os.unlink(tmp)

def detect_scale(img_w: int) -> tuple[float, int]:
    try:
        r = subprocess.run(["osascript", "-e", 'tell application "Finder" to get bounds of window of desktop'],
                            capture_output=True, text=True)
        parts = [int(v.strip()) for v in r.stdout.strip().split(",")]
        logical_w = parts[2]
        return img_w / logical_w, logical_w
    except Exception:
        return 1.0, img_w

FONT_PATH = "/System/Library/Fonts/Helvetica.ttc"