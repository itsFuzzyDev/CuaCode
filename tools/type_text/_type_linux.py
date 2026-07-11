import subprocess

def type_text(text: str, delay: float = 0.01):
    subprocess.run(["xdotool", "type", "--delay", str(int(delay * 1000)), text], check=True)