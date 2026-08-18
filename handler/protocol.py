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
        # True for exactly as long as a turn is running. It is what tells a
        # chat envelope whether it is the next message or a mid-turn one, and
        # nothing else reads it.
        self._running = threading.Event()
        # Messages typed while a turn was in flight, oldest first. Envelopes
        # rather than strings: one that is never spoken into the round has to
        # go back to the inbox intact and become an ordinary turn.
        self._steer: list[Envelope] = []
        self._steer_lock = threading.Lock()
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
                # A message typed while the turn is still going. Held out here
                # for the same reason, and -- unlike cancel -- deliberately not
                # queued: the main loop would read it after the run and answer
                # it as a second turn, which is the behaviour this replaces. The
                # loop drains it between tool calls instead and speaks it into
                # the round that is already happening. Anything still unspoken
                # when the run ends is put back on the inbox by end_run, so a
                # message is delayed at worst and never dropped.
                elif action == "chat" and self._running.is_set():
                    with self._steer_lock: self._steer.append(env)
                    # Shaped like chat_received and for the same purpose -- an
                    # acknowledgement, not a state. A `state` field here would
                    # be folded into the snapshot and move a frontend off
                    # whatever the run is actually doing.
                    self.send("status", {"type": "chat_queued"}, id_=env.id)
                    continue

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
        # Last, and only after the two clears: the flag is what makes the
        # reader hold chat envelopes back, and holding one back before the run
        # it belongs to has started would strand it for a whole turn.
        self._running.set()

    def end_run(self):
        """Close the turn and hand back whatever was typed during it.

        Anything the loop never got to -- a message that arrived after the last
        tool call, or during a round that had none -- goes on the inbox now and
        is answered as an ordinary next turn. This is the only path off the
        steer list other than take_steer, which is what makes "queued" a delay
        rather than a place messages go to die. Safe to call twice.
        """
        self._running.clear()
        with self._steer_lock: left, self._steer = self._steer, []
        for env in left: self.inbox.put(env)

    def take_steer(self) -> list[str]:
        """Everything typed since the last drain, oldest first, and clear.

        Handed to generate() as a callable so the loop can ask at the one place
        a user message is legal for every provider -- straight after a round's
        tool results -- rather than the loop being interrupted from outside.
        """
        with self._steer_lock: got, self._steer = self._steer, []
        return [t for t in ((e.data or {}).get("text") or "" for e in got) if t.strip()]

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
