import subprocess

def active_app_name() -> str:
    try:
        r = subprocess.run(["xdotool", "getactivewindow", "getwindowname"],
                            capture_output=True, text=True)
        return r.stdout.strip().replace(" ", "_")[:40] or "unknown"
    except Exception:
        return "unknown"

def capture():
    from PIL import Image
    import mss
    with mss.mss() as sct:
        shot = sct.grab(sct.monitors[1])
        return Image.frombytes("RGB", shot.size, shot.rgb)

def detect_scale(img_w: int) -> tuple[float, int]:
    return 1.0, img_w  # X11 mss generally reports logical already

FONT_PATH = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"