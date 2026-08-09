import ctypes, time

INPUT_KEYBOARD = 1
KEYEVENTF_UNICODE = 0x0004
KEYEVENTF_KEYUP = 0x0002

class KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", ctypes.c_ushort), ("wScan", ctypes.c_ushort),
                ("dwFlags", ctypes.c_ulong), ("time", ctypes.c_ulong),
                ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong))]

class INPUT(ctypes.Structure):
    _fields_ = [("type", ctypes.c_ulong), ("ki", KEYBDINPUT)]

def _send_char(ch: str, key_up: bool):
    flags = KEYEVENTF_UNICODE | (KEYEVENTF_KEYUP if key_up else 0)
    inp = INPUT(type=INPUT_KEYBOARD, ki=KEYBDINPUT(0, ord(ch), flags, 0, None))
    ctypes.windll.user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(inp))

def type_text(text: str, delay: float = 0.01):
    for ch in text:
        _send_char(ch, key_up=False)
        _send_char(ch, key_up=True)
        if delay: time.sleep(delay)