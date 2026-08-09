import subprocess

def move(x: int, y: int):
    subprocess.run(["xdotool", "mousemove", str(x), str(y)], check=True)