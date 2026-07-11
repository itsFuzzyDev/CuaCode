import ctypes

_LEFT = (0x0002, 0x0004)     # MOUSEEVENTF_LEFTDOWN / LEFTUP
_RIGHT = (0x0008, 0x0010)    # RIGHTDOWN / RIGHTUP
_MIDDLE = (0x0020, 0x0040)   # MIDDLEDOWN / MIDDLEUP
_FLAGS = {"left": _LEFT, "right": _RIGHT, "middle": _MIDDLE}

def click(x: int, y: int, button: str = "left", clicks: int = 1):
    ctypes.windll.user32.SetCursorPos(int(x), int(y))
    down, up = _FLAGS[button]
    for _ in range(clicks):
        ctypes.windll.user32.mouse_event(down, 0, 0, 0, 0)
        ctypes.windll.user32.mouse_event(up, 0, 0, 0, 0)