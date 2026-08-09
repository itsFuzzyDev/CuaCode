"""Stopping something that is already running.

The loop could always be stopped between steps -- between streamed chunks, and
between tool calls -- because those are the moments it is holding nothing. That
covers a run that is thinking, and misses the one case where somebody actually
reaches for the key: a call that has been running for ninety seconds and is not
going to end on its own.

The gap is that dispatch() is an ordinary blocking call. Nothing in the loop can
observe a cancel while it is inside one, because the loop *is* inside it. So the
handler runs on its own thread and the loop watches the flags instead of the
handler. The handler is unchanged; the thread it runs on is the whole of what
makes it interruptible.

Two ways out, and the difference is bookkeeping, not mechanism:

  cancelled  the user is done with this turn. The round rewinds past the call,
             so it needs no result -- an assistant message whose tool_calls have
             no matching results is what rewinding exists to avoid, and rewinding
             is what makes a missing result fine.
  detached   the user wants the turn to carry on without waiting. The thread
             keeps its box, and whatever it eventually writes there is collected
             by the background registry.

A tool that wants to stop early is told through the token in ctx. One that
ignores it is abandoned, which is why these threads are daemons: a handler
sitting in a socket read must never be the reason the app will not exit.
"""
import threading

POLL = 0.02          # how often the flags are read while a call is in flight


class Token:
    """The cancel a running tool can see.

    Handed to handlers through ctx["cancel"], so a long call can give up on its
    own terms -- shell kills its process group rather than leaving one running
    past the turn it belonged to. Looking at it is optional: a handler that
    never does still works, it just cannot finish early.
    """
    __slots__ = ("_event",)

    def __init__(self): self._event = threading.Event()
    def cancel(self): self._event.set()
    def cancelled(self) -> bool: return self._event.is_set()
    def wait(self, timeout=None) -> bool: return self._event.wait(timeout)

    # Deliberately no __bool__. Either answer is a trap: truthy-when-cancelled
    # makes `token or Token()` silently swap a live token for a fresh one, and
    # truthy-when-present makes `if token:` read as "has it been cancelled" to
    # everyone who did not write it. Callers say cancelled() and mean it.


class Call:
    """One handler running on its own thread, plus the box it writes to.

    The box outlives the wait. That is the point: whoever backgrounds the call
    keeps a reference to this object and reads the result whenever it lands,
    long after the loop that started it moved on.
    """

    def __init__(self, fn, token: "Token" = None):
        # `token is None`, never `token or ...`: the caller's token is the one
        # already sitting in the ctx the handler will read, and quietly
        # replacing it would leave the loop cancelling an object nobody is
        # watching -- a cancel that reports success and does nothing.
        self.token = Token() if token is None else token
        self._fn = fn
        # done is written last, after result, so a reader that sees done sees
        # a result too. Nothing else touches these, which is why no lock.
        self.box: dict = {}
        self.thread = threading.Thread(target=self._run, daemon=True)

    def _run(self):
        try:
            self.box["result"] = self._fn()
        except BaseException as e:
            # Shaped like every other failed call, because that is what it is
            # from the model's side. dispatch() already catches Exception; this
            # is the layer under it, and it must not lose a job to a traceback
            # nobody is standing under.
            self.box["result"] = {"error": str(e)}
        finally:
            self.box["done"] = True

    def start(self) -> "Call":
        self.thread.start()
        return self

    @property
    def done(self) -> bool: return bool(self.box.get("done"))

    @property
    def result(self): return self.box.get("result")


def ctx_with(ctx, token: Token):
    """A per-call copy of ctx carrying this call's cancel token.

    A copy rather than a mutation: ctx is shared, and a background job still
    holding it would otherwise watch the next call's token appear underneath it
    and stop when that one was cancelled. The class is preserved because ctx is
    a dict subclass with properties on it that tools read.

    Two keys for one signal, because there are two kinds of reader. A tool
    watches "cancel" and decides what stopping means for it -- shell kills its
    process group. A nested agent loop takes "cancelled" and passes it straight
    to generate() as its stop predicate, which is how cancelling a run reaches
    the subagent inside the tool call inside it. The token is per call, so a
    subagent that was backgrounded keeps running when a later turn is cancelled:
    it is no longer the call anyone is cancelling.
    """
    try: out = type(ctx)(ctx) if ctx is not None else {}
    except Exception: out = dict(ctx or {})
    try:
        out["cancel"] = token
        out["cancelled"] = token.cancelled
    except Exception: return ctx
    return out


def run(fn, stop=None, detach=None, token: Token = None, poll: float = POLL) -> tuple[str, object]:
    """Run fn on a thread, watching both flags while it does.

    Returns ("done", result) when it finished, ("cancelled", call) when stop()
    went true, ("detached", call) when the detach event was set. The Call comes
    back in the latter two because the thread is still running and the caller is
    what decides where it goes next.

    stop cancels the token on the way out, so a cooperative tool gets the chance
    to stop rather than grind on toward a result nobody will read. detach does
    not -- the entire point of backgrounding is that the work continues.

    detach is a threading.Event rather than a predicate, and it is cleared here
    on entry. A press that arrived before this call started was aimed at
    something else, or at nothing; honouring it would background whatever ran
    next, which is never what the person pressing the key meant.
    """
    stop = stop or (lambda: False)
    if detach is not None: detach.clear()
    call = Call(fn, token).start()
    while True:
        # join returns the moment the thread ends, so the poll interval is a
        # ceiling on how long a flag goes unnoticed, not on how long a fast
        # call takes.
        call.thread.join(poll)
        if call.done: return "done", call.result
        if stop():
            call.token.cancel()
            return "cancelled", call
        if detach is not None and detach.is_set():
            detach.clear()
            return "detached", call
