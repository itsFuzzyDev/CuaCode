import subprocess

from tools._display import cursor, env

_BUTTONS = {"left": "1", "right": "3", "middle": "2"}
# Silent: xdotool writes nothing on success, but its error goes to stderr, which
# is the terminal the TUI is drawing in.
_QUIET = dict(stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

def _xdo(*args):
    subprocess.run(["xdotool", *args], check=True, env=env(), **_QUIET)

def click(x: int, y: int, button: str = "left", clicks: int = 1):
    _xdo("mousemove", str(x), str(y))
    _xdo("click", "--repeat", str(clicks), _BUTTONS[button])
