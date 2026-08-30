import ctypes

_LEFT = (0x0002, 0x0004)     # MOUSEEVENTF_LEFTDOWN / LEFTUP
_RIGHT = (0x0008, 0x0010)    # RIGHTDOWN / RIGHTUP
_MIDDLE = (0x0020, 0x0040)   # MIDDLEDOWN / MIDDLEUP
_FLAGS = {"left": _LEFT, "right": _RIGHT, "middle": _MIDDLE}

class _POINT(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]

def cursor():
    """Where the pointer actually is, or None if Windows will not say.

    SetCursorPos returns a failure code the callers below do not check, and it
    is not the only way this goes quiet: a UIPI block, or the secure desktop
    being up, leaves the pointer where it was with nothing raised. Reading it
    back is the same check the macOS path does, minus the wait -- SetCursorPos
    is synchronous, so there is no settling to poll for.
    """
    p = _POINT()
    if not ctypes.windll.user32.GetCursorPos(ctypes.byref(p)):
        return None
    return p.x, p.y


def click(x: int, y: int, button: str = "left", clicks: int = 1):
    ctypes.windll.user32.SetCursorPos(int(x), int(y))
    down, up = _FLAGS[button]
    for _ in range(clicks):
        ctypes.windll.user32.mouse_event(down, 0, 0, 0, 0)
        ctypes.windll.user32.mouse_event(up, 0, 0, 0, 0)