import json, os, queue, sys, threading, time, uuid
from dataclasses import dataclass
from typing import Callable, Optional


@dataclass
class Envelope:
    type: str
    id: str
    data: dict

    def to_dict(self) -> dict: return {"type": self.type, "id": self.id, "data": self.data}

    @classmethod
    def from_dict(cls, raw: dict) -> "Envelope":
        return cls(type=raw.get("type", ""), id=raw.get("id", ""), data=raw.get("data", {}) or {})


class IPC:
    def __init__(self):
        self.inbox: queue.Queue[Envelope] = queue.Queue()
        self._lock = threading.Lock()
        self._pending: dict[str, Callable[[Envelope], None]] = {}
        self.terminal_info: dict = {}
        _sp = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'system_prompt.txt')
        self.messages: list[dict] = [{"role": "system", "content": open(_sp, 'r').read()}]
        threading.Thread(target=self._read_loop, daemon=True).start()

    def _read_loop(self):
        for line in sys.stdin:
            line = line.strip()
            if not line: continue
            try:
                env = Envelope.from_dict(json.loads(line))
                
                # important, sets terminal info directly from go instead of doing some really weird loops through python to get it, works much better.
                if env.type == "terminal": self.terminal_info = env.data #; continue # only put if you dont want to recieve any terminal data into the main loop handler.

                # If someone is waiting for this id via call(), hand it to them.
                # Otherwise queue it for normal polling.
                cb = self._pending.pop(env.id, None)
                if cb: cb(env)
                else: self.inbox.put(env)
            except json.JSONDecodeError: pass

    def send(self, type_: str, data: Optional[dict] = None, id_: Optional[str] = None) -> str:
        """Send an envelope. Returns the id (generated if not provided)."""
        id_ = id_ or str(uuid.uuid4())
        env = Envelope(type=type_, id=id_, data=data or {})
        with self._lock:
            sys.stdout.write(json.dumps(env.to_dict()) + "\n")
            sys.stdout.flush()
        return id_

    def reply(self, to: Envelope, type_: str, data: Optional[dict] = None) -> str:
        """Reply to an envelope, echoing its id back."""
        return self.send(type_, data, id_=to.id)

    def call(self, type_: str, data: Optional[dict] = None, timeout: Optional[float] = None) -> Optional[Envelope]:
        """Send and block until a response with the same id arrives."""
        event = threading.Event()
        result: list[Optional[Envelope]] = [None]

        def cb(env: Envelope):
            result[0] = env
            event.set()

        id_ = self.send(type_, data)
        self._pending[id_] = cb
        if event.wait(timeout=timeout): return result[0]
        self._pending.pop(id_, None)
        return None

    def poll(self) -> list[Envelope]:
        cmds = []
        while not self.inbox.empty(): cmds.append(self.inbox.get())
        return cmds

    def poll_one(self, timeout: Optional[float] = None) -> Optional[Envelope]:
        try: return self.inbox.get(timeout=timeout)
        except queue.Empty: return None

# example of protocol, have otherside send stuff and get these responses below
if __name__ == "__main__":
    ipc = IPC()
    ipc.send("status", {"state": "startup"})
    while True:
        for env in ipc.poll():
            if env.type != "cmd":
                continue
            action = env.data.get("action")
            if action == "stop":
                sys.exit(0)
            if action == "say_hi":
                ipc.reply(env, "status", {"state": "running", "text": "hi!"})
            if action == "inject":
                ipc.reply(env, "status", {"state": "injected", "text": env.data.get("text")})
        ipc.send("action", {"action": "do_abc", "data": [1, 2, 3, 4]})
        time.sleep(1)
