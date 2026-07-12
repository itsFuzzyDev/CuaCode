import sys, os, time
sys.path.insert(0, os.path.dirname(__file__))
from handler.protocol import IPC

ipc = IPC()
ipc.send("status", {"state": "ready"})

while True:
    for cmd in ipc.poll():
        action = cmd.get("data", {}).get("action")
        if action == "stop":
            ipc.send("status", {"state": "stopped"})
            sys.exit(0)
        elif action == "pause":
            ipc.send("status", {"state": "paused"})
        elif action == "inject":
            ipc.send("you injected", {"state": "injected", "text": cmd["data"].get("text")})
        else:
            ipc.send("status", {"state": "unknown_cmd", "action": action})
    time.sleep(0.5)
