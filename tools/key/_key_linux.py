import subprocess

_ALIASES = {"cmd": "super", "command": "super", "control": "ctrl", "option": "alt",
            "return": "Return", "enter": "Return", "escape": "Escape", "esc": "Escape",
            "space": "space", "tab": "Tab", "delete": "Delete", "backspace": "BackSpace",
            "up": "Up", "down": "Down", "left": "Left", "right": "Right"}

def press(combo: str):
    parts = [p.strip().lower() for p in combo.split("+")]
    mapped = [_ALIASES.get(p, p) for p in parts]
    xdotool_combo = "+".join(mapped)
    subprocess.run(["xdotool", "key", xdotool_combo], check=True)