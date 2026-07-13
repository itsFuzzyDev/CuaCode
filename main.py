import sys, os, time, random
sys.path.insert(0, os.path.dirname(__file__))
from handler.protocol import IPC, Envelope
from handler.agent.main import generate
from tools._parser.FromProvider import parse_tool_calls

ipc = IPC()
ipc.send("status", {"state": "ready"})
Ctx = type('Ctx', (dict,), {'self_identity': property(lambda s: s.get("frontmost_app")), 'session_dir': property(lambda s: s.get("session_dir"))})
ctx = Ctx(ipc.terminal_info)

while True:
    for env in ipc.poll():
        if env.type == "terminal":
            ipc.reply(env, "status", {"terminal": ipc.terminal_info})
            ipc.terminal_info = env.data
            ctx = Ctx(ipc.terminal_info)
            continue
        if env.type != "cmd": continue
        if env.data.get("action") == "stop":
            ipc.reply(env, "status", {"state": "stopped"})
            sys.exit(0)
        elif env.data.get("action") == "chat":
            text = env.data.get("text", "")
            ipc.messages.append({"role": "user", "content": text})
            ipc.reply(env, "status", {"type": "chat_received"})
            try:
                for chunk in generate(messages=ipc.messages, ctx=ctx):
                    typ = chunk.get("type")
                    if typ == "done":
                        ipc.messages = chunk.get("messages", ipc.messages)
                        ipc.reply(env, "token", {"state": "done", "token": "done", "status": "done"})
                    elif typ == "tool_calls":
                        ipc.reply(env, "token", {"state": "tool_calls", "token": chunk.get("text"), "status": "running"})
                    else:
                        ipc.reply(env, "token", {"state": typ, "token": chunk.get("text"), "status": "running"})
            except Exception as e:
                ipc.reply(env, "token", {"state": "error", "token": str(e), "status": "error"})
    time.sleep(0.001)