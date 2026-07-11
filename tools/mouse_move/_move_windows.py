import ctypes

def move(x: int, y: int):
    ctypes.windll.user32.SetCursorPos(int(x), int(y))