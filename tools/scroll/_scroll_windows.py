import ctypes

MOUSEEVENTF_WHEEL = 0x0800
MOUSEEVENTF_HWHEEL = 0x01000

def scroll(x: int, y: int, dx: int, dy: int):
    ctypes.windll.user32.SetCursorPos(int(x), int(y))
    if dy: ctypes.windll.user32.mouse_event(MOUSEEVENTF_WHEEL, 0, 0, int(dy), 0)
    if dx: ctypes.windll.user32.mouse_event(MOUSEEVENTF_HWHEEL, 0, 0, int(dx), 0)