import ctypes

_VK = {
    "a": 0x41, "b": 0x42, "c": 0x43, "d": 0x44, "e": 0x45, "f": 0x46, "g": 0x47,
    "h": 0x48, "i": 0x49, "j": 0x4A, "k": 0x4B, "l": 0x4C, "m": 0x4D, "n": 0x4E,
    "o": 0x4F, "p": 0x50, "q": 0x51, "r": 0x52, "s": 0x53, "t": 0x54, "u": 0x55,
    "v": 0x56, "w": 0x57, "x": 0x58, "y": 0x59, "z": 0x5A,
    "0": 0x30, "1": 0x31, "2": 0x32, "3": 0x33, "4": 0x34, "5": 0x35,
    "6": 0x36, "7": 0x37, "8": 0x38, "9": 0x39,
    "return": 0x0D, "enter": 0x0D, "tab": 0x09, "space": 0x20, "delete": 0x2E,
    "escape": 0x1B, "esc": 0x1B, "up": 0x26, "down": 0x28, "left": 0x25, "right": 0x27,
    "backspace": 0x08,
}
_MODS = {"ctrl": 0x11, "control": 0x11, "alt": 0x12, "shift": 0x10, "cmd": 0x5B, "win": 0x5B}

KEYEVENTF_KEYUP = 0x0002

def press(combo: str):
    parts = [p.strip().lower() for p in combo.split("+")]
    *mods, key = parts
    vk = _VK.get(key)
    if vk is None: raise ValueError(f"unknown key: {key}")
    mod_codes = [_MODS[m] for m in mods]

    for m in mod_codes: ctypes.windll.user32.keybd_event(m, 0, 0, 0)
    ctypes.windll.user32.keybd_event(vk, 0, 0, 0)
    ctypes.windll.user32.keybd_event(vk, 0, KEYEVENTF_KEYUP, 0)
    for m in reversed(mod_codes): ctypes.windll.user32.keybd_event(m, 0, KEYEVENTF_KEYUP, 0)