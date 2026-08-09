import json, queue, sys, threading, time, uuid
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
        self._cancel = threading.Event()
        # Not a flag the main loop owns: it is handed straight to generate(),
        # which clears it as it consumes it, so one press moves one call.
        self.background = threading.Event()
        # Set when stdin hits EOF -- the frontend that owned the pipe is gone.
        # The main loop checks it so a dead frontend cannot leave an orphan
        # worker polling an empty inbox forever.
        self._eof = threading.Event()
        threading.Thread(target=self._read_loop, daemon=True).start()

    def _read_loop(self):
        for line in sys.stdin:
            line = line.strip()
            if not line: continue
            try:
                env = Envelope.from_dict(json.loads(line))
                
                # important, sets terminal info directly from go instead of doing some really weird loops through python to get it, works much better.
                if env.type == "terminal": self.terminal_info = env.data #; continue # only put if you dont want to recieve any terminal data into the main loop handler.

                # Cancel is flagged here, on the reader thread, because the main
                # loop is blocked inside generate() for the whole run and will
                # not call poll() again until it finishes. The envelope is still
                # queued below so the main loop can acknowledge it afterwards.
                action = (env.data or {}).get("action") if env.type == "cmd" else None
                if action == "cancel": self._cancel.set()
                # Same reasoning, same thread: the main loop is inside
                # generate() for the whole run, so anything meant to reach a
                # call already in flight has to be flagged from out here.
                elif action == "background": self.background.set()

                # If someone is waiting for this id via call(), hand it to them.
                # Otherwise queue it for normal polling.
                cb = self._pending.pop(env.id, None)
                if cb: cb(env)
                else: self.inbox.put(env)
            except json.JSONDecodeError: pass
        # stdin closed: the frontend that spawned us is gone (crashed, was
        # killed, or force-quit without sending stop). Signal the main loop so
        # it can shut down instead of running on as an orphan.
        self._eof.set()

    def eof(self) -> bool:
        """True once stdin has closed -- the frontend that spawned us is gone."""
        return self._eof.is_set()

    def begin_run(self):
        """Drop anything that arrived while idle, so it can't act on the next
        run. A background press with nothing running was aimed at nothing."""
        self._cancel.clear()
        self.background.clear()

    def cancelled(self) -> bool:
        """True once a cancel has arrived for the run in flight."""
        return self._cancel.is_set()

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

    def call(self, type_: str, data: Optional[dict] = None, timeout: Optional[float] = None,
             stop: Optional[Callable[[], bool]] = None) -> Optional[Envelope]:
        """Send and block until a response with the same id arrives.

        stop makes the wait abandonable. A permission prompt is asked with no
        timeout on purpose -- a question the user only gets to tomorrow is still
        answered tomorrow -- but "no timeout" and "cannot be called off" are
        different things, and without this a cancel arriving while a prompt is
        up would hang the run it was meant to end. Giving up returns None, which
        every caller already reads as "no answer".
        """
        event = threading.Event()
        result: list[Optional[Envelope]] = [None]

        def cb(env: Envelope):
            result[0] = env
            event.set()

        id_ = self.send(type_, data)
        self._pending[id_] = cb
        if stop is None:
            if event.wait(timeout=timeout): return result[0]
        else:
            deadline = None if timeout is None else time.monotonic() + timeout
            while not stop():
                if event.wait(timeout=0.05): return result[0]
                if deadline is not None and time.monotonic() >= deadline: break
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
