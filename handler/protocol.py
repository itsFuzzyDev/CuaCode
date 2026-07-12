import sys, json, threading, queue, uuid, time

class IPC:
    def __init__(self):
        self.inbox = queue.Queue()
        self.lock = threading.Lock()
        threading.Thread(target=self._read_loop, daemon=True).start()

    def _read_loop(self):
        for line in sys.stdin:
            line = line.strip()
            if not line: continue
            try: self.inbox.put(json.loads(line))
            except json.JSONDecodeError: pass

    def send(self, type_, data=None):
        with self.lock:
            sys.stdout.write(json.dumps({"type": type_, "id": str(uuid.uuid4()), "data": data or {}}) + "\n")
            sys.stdout.flush()

    def poll(self):
        cmds = []
        while not self.inbox.empty(): cmds.append(self.inbox.get())
        return cmds

    def poll_one(self, timeout=None):
        try: return self.inbox.get(timeout=timeout)
        except queue.Empty: return None


# if you would rather directly talk to the protocol instead of the main and you would like to learn how to call it heres an example for you:
if __name__ == "__main__":
    # to create a protocol, you can initalize
    ipc = IPC()
    # then send a message that will announce startup, make sure you capture it on the other side.
    ipc.send("status", {"state": "startup"})
    while True:
        for cmd in ipc.poll():
            action = cmd.get("data", {}).get("action")
            # now here you can do customa ctions
            if action == "stop": sys.exit(0)
            if action == "say_hi": ipc.send("status", {"state": "running", "text": "hi!"})
            # or 'injection' that will return the text.
            if action == "inject": ipc.send("status", {"state": "injected", "text": cmd["data"].get("text")})
        ipc.send("action", {"action": "do_abc", "data": [1,2,3,4]})
        time.sleep(1)