import ctypes

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

def move(x: int, y: int):
    ctypes.windll.user32.SetCursorPos(int(x), int(y))