import subprocess

from tools._display import cursor, env

def move(x: int, y: int):
    # Silent: xdotool's errors go to stderr, which is the terminal the TUI draws in.
    subprocess.run(["xdotool", "mousemove", str(x), str(y)], check=True, env=env(),
                   stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                   stderr=subprocess.DEVNULL)
