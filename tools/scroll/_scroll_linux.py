import subprocess

def scroll(x: int, y: int, dx: int, dy: int):
    subprocess.run(["xdotool", "mousemove", str(x), str(y)], check=True)
    button = 5 if dy > 0 else 4  # 4=up, 5=down
    clicks = abs(int(dy / 20)) or 1
    subprocess.run(["xdotool", "click", "--repeat", str(clicks), str(button)], check=True)
    if dx:
        hbutton = 7 if dx > 0 else 6  # 6=left, 7=right
        hclicks = abs(int(dx / 20)) or 1
        subprocess.run(["xdotool", "click", "--repeat", str(hclicks), str(hbutton)], check=True)