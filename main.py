import sys, os, time
sys.path.insert(0, os.path.dirname(__file__))
from handler.protocol import IPC, Envelope

ipc = IPC()
ipc.send("status", {"state": "ready"})

while True:
    for env in ipc.poll():
        if env.type != "cmd": continue
        action = env.data.get("action")
        if action == "stop":
            ipc.reply(env, "status", {"state": "stopped"})
            sys.exit(0)
        elif action == "pause":
            ipc.reply(env, "status", {"state": "paused"})
        elif action == "inject":
            ipc.reply(env, "status", {"state": "injected", "text": env.data.get("text"), "hi": "yes"})
        else:
            ipc.reply(env, "status", {"state": "unknown_cmd", "action": action})
    time.sleep(0.5)
