#!/usr/bin/env python3
"""The desktop's bounding box, where a capture starts, and the portal's file.

The thing being pinned: a Linux capture holds every monitor rather than the primary
one. That makes the image's top-left corner the virtual desktop's own corner, which is
not (0, 0) when a monitor sits left of or above the primary one, and the grid labels
are screen coordinates that a click can be aimed at.

No display is needed: the monitor list is set directly, and the portal's file is
simulated on a temp directory.

    python3 tests/test_capture_linux.py
"""
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT / "tools" / "screenshot"), str(ROOT)]

import _capture_linux as cap

FAILS = []


def check(name, cond, detail=""):
    print(("ok   " if cond else "FAIL ") + name + (f"  -> {detail}" if not cond else ""))
    if not cond: FAILS.append(name)


def monitor(x, y, w=1920, h=1080, primary=False, name=""):
    return {"x": x, "y": y, "width": w, "height": h, "primary": primary, "name": name}


# 1. Two monitors side by side: the capture covers both, and starts at the left one.
cap._MONITORS = [monitor(0, 0, primary=True, name="DP-4"), monitor(1920, 0, name="HDMI-2")]
check("the box covers both monitors", cap._virtual() == (0, 0, 3840, 1080), cap._virtual())
check("and it starts at the top-left of the desktop", cap.origin() == (0, 0), cap.origin())

# 2. A monitor left of the primary: the corner is negative, and the labels follow it.
cap._MONITORS = [monitor(0, 0, primary=True, name="DP-4"), monitor(-1920, 0, name="HDMI-2")]
check("a monitor to the left moves the corner with it", cap._virtual() == (-1920, 0, 3840, 1080),
      cap._virtual())

# 3. A monitor above, at a different height, is still one box.
cap._MONITORS = [monitor(0, 0, primary=True), monitor(1920, -1080)]
check("a monitor above extends the box upward", cap._virtual() == (0, -1080, 3840, 2160),
      cap._virtual())

# 4. Scale: framebuffer pixels over logical pixels, and only when there is a difference.
cap._MONITORS = [monitor(0, 0, primary=True), monitor(1920, 0)]
check("a capture the size of the desktop is unscaled", cap.detect_scale(3840, 1080) == (1.0, 3840, 1080),
      cap.detect_scale(3840, 1080))
check("a HiDPI capture is scaled back to logical points", cap.detect_scale(7680, 2160) == (2.0, 3840, 1080),
      cap.detect_scale(7680, 2160))

# 5. A black grab is what Wayland hands back, and is not a picture of the screen.
from PIL import Image
check("an all-black capture is rejected", cap._blank(Image.new("RGB", (8, 8))))
check("a capture with something in it is kept",
      not cap._blank(Image.new("RGB", (8, 8), (0, 0, 1))))

# 6. The portal creates its file empty and fills it after. Only a settled, non-empty
#    file is the picture; an empty one used to be read and failed.
folder = Path(tempfile.mkdtemp(prefix="cuacode-shot-"))
(folder / "Screenshot-1.png").write_bytes(b"")
check("a file the portal has not written yet is not read", cap._wait_newest(folder, set(), timeout=0.6) is None)
(folder / "Screenshot-1.png").write_bytes(Image.new("RGB", (4, 4), (9, 9, 9)).tobytes())
check("the file is read once it holds something", cap._wait_newest(folder, set(), timeout=2) is not None)
check("a file that was already there is not taken for this capture",
      cap._wait_newest(folder, {"Screenshot-1.png"}, timeout=0.6) is None)

print()
if FAILS:
    print("FAILED: " + ", ".join(FAILS))
    sys.exit(1)
print("all linux capture checks passed")
