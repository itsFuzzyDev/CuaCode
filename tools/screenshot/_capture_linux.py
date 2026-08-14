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

def detect_scale(img_w: int, img_h: int) -> tuple[float, int, int]:
    # X11 through mss grabs the same pixel grid xdotool moves the cursor on, so
    # capture pixels and click coordinates are already the same unit.
    return 1.0, img_w, img_h

FONT_PATH = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"