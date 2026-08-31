import subprocess, tempfile, os

def active_app_name() -> str:
    """Whoever is in front, for naming the archived shot.

    Straight from the workspace rather than through `osascript -e`: this is
    called once per file written, twice per capture, and an AppleEvent round
    trip to System Events is 166ms to answer a question AppKit answers in
    microseconds. The AppleScript stays as the fallback for a machine where
    pyobjc is not importable.
    """
    try:
        from tools._appkit import appkit
        name = appkit().NSWorkspace.sharedWorkspace().frontmostApplication().localizedName()
        if name: return name.replace(" ", "_")
    except Exception:
        pass
    try:
        r = subprocess.run(["osascript", "-e",
            'tell application "System Events" to get name of first process whose frontmost is true'],
            capture_output=True, text=True)
        return r.stdout.strip().replace(" ", "_") or "unknown"
    except Exception:
        return "unknown"

def capture():
    from PIL import Image
    # BMP, not PNG: the file lives for a millisecond and is read back right
    # here, so compression is pure cost (screencapture 130ms to deflate, PIL
    # more to inflate). The PNG the agent gets is encoded once, later.
    with tempfile.NamedTemporaryFile(suffix=".bmp", delete=False) as f: tmp = f.name
    try:
        # -D 1 pins the shot to the main display. Left implicit, screencapture
        # writes one file per attached display and only the first lands on the
        # path we hand it -- so a second monitor changed what "the screenshot"
        # meant. Every coordinate below is measured from the main display's
        # origin anyway, which is the only one CGEvent clicks can reach here.
        subprocess.run(["screencapture", "-x", "-D", "1", "-t", "bmp", tmp], check=True)
        img = Image.open(tmp)
        img.load()  # decode before the file goes away; Image.open is lazy
        return img
    finally:
        os.unlink(tmp)

def detect_scale(img_w: int, img_h: int) -> tuple[float, int, int]:
    """Physical capture pixels -> logical points, straight from the window server.

    Quartz reports both units for the main display, so the ratio is exact and
    costs no permission prompt. The AppleScript route this replaces asked Finder
    for the desktop bounds, which meant two silent ways to be wrong: denied
    Automation access fell back to a scale of 1.0 and left every click landing
    at half its intended position, and a second monitor made Finder report the
    union of all screens, so the scale came back under 1.0 and was dropped.

    A capture that does not match the framebuffer is not scaled by guesswork --
    a grid labelled in the wrong unit is worse than no screenshot, because the
    agent cannot see that it is wrong.
    """
    import Quartz as _Q
    did = _Q.CGMainDisplayID()
    logical_w, logical_h = int(_Q.CGDisplayPixelsWide(did)), int(_Q.CGDisplayPixelsHigh(did))
    mode = _Q.CGDisplayCopyDisplayMode(did)
    phys_w, phys_h = int(_Q.CGDisplayModeGetPixelWidth(mode)), int(_Q.CGDisplayModeGetPixelHeight(mode))
    if not logical_w or not logical_h:
        raise RuntimeError("main display reported a zero size; cannot map screenshot pixels to click coordinates")
    if (img_w, img_h) != (phys_w, phys_h):
        raise RuntimeError(
            f"screenshot is {img_w}x{img_h} but the main display framebuffer is {phys_w}x{phys_h}; "
            "refusing to guess the scale factor, because clicks would land off target")
    return img_w / logical_w, logical_w, logical_h

FONT_PATH = "/System/Library/Fonts/Helvetica.ttc"