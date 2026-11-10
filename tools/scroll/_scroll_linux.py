import subprocess

from tools._display import env

# Silent: xdotool's errors go to stderr, which is the terminal the TUI draws in.
_QUIET = dict(check=True, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
              stderr=subprocess.DEVNULL)

def _xdo(*args):
    subprocess.run(["xdotool", *args], env=env(), **_QUIET)

def scroll(x: int, y: int, dx: int, dy: int):
    _xdo("mousemove", str(x), str(y))
    button = 5 if dy > 0 else 4  # 4=up, 5=down
    clicks = abs(int(dy / 20)) or 1
    _xdo("click", "--repeat", str(clicks), str(button))
    if dx:
        hbutton = 7 if dx > 0 else 6  # 6=left, 7=right
        hclicks = abs(int(dx / 20)) or 1
        _xdo("click", "--repeat", str(hclicks), str(hbutton))
