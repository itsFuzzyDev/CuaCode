import subprocess

_BUTTONS = {"left": "1", "right": "3", "middle": "2"}

def click(x: int, y: int, button: str = "left", clicks: int = 1):
    subprocess.run(["xdotool", "mousemove", str(x), str(y)], check=True)
    subprocess.run(["xdotool", "click", "--repeat", str(clicks), _BUTTONS[button]], check=True)