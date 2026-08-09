"""Tool calls that outlive the round that started them.

Two things ask for this and they are not the same request. The model can start a
call in the background because it already knows the call is slow and has other
work to do meanwhile -- a build, a test suite, a long fetch. The user can push a
call that is *already running* into the background because it turned out slow
and they are tired of watching it. The second is why this cannot just be a flag
in an input schema: by the time anyone wants it, the call has started.

Both doors open onto the same Job, holding the same interrupt.Call. A job does
not know which way it came in.

Nothing is pushed at the model. A finished job sits here until something asks,
and what asks is either the model calling the background tool or the loop
dropping a one-line notice between rounds. A result injected into the middle of
a round would arrive while the model is partway through something else, and the
notice-then-fetch shape lets it read the thing when it is ready for it.
"""
import itertools, threading, time

from handler.agent.interrupt import Call

MAX_KEPT = 50          # finished jobs held for collection; running ones are never dropped
ARG_PREVIEW = 120      # chars of an argument kept for display


def _short(args: dict) -> dict:
    """Arguments trimmed to something a status line can hold. The full ones are
    already in the transcript; this is a label, not a record."""
    out = {}
    for k, v in (args or {}).items():
        if isinstance(v, str) and len(v) > ARG_PREVIEW: out[k] = v[:ARG_PREVIEW] + "..."
        elif isinstance(v, (str, int, float, bool)) or v is None: out[k] = v
        else: out[k] = f"<{type(v).__name__}>"
    return out


class Job:
    """One backgrounded call.

    State is read off the Call rather than copied, so there is exactly one place
    a result exists and no second copy to keep in step with it.
    """

    def __init__(self, jid: str, name: str, args: dict, call: Call, started: float):
        self.id, self.name, self.args, self.call = jid, name, args, call
        self.started, self.finished = started, None
        # Whether the model has been handed this result. Only used to decide
        # what the between-rounds notice mentions -- output can be read again as
        # many times as it likes.
        self.reported = False

    def _tick(self):
        if self.finished is None and self.call.done: self.finished = time.monotonic()

    @property
    def state(self) -> str:
        self._tick()
        if not self.call.done:
            return "killing" if self.call.token.cancelled() else "running"
        r = self.call.result
        return "error" if isinstance(r, dict) and r.get("error") else "done"

    @property
    def elapsed(self) -> float:
        self._tick()
        return round((self.finished or time.monotonic()) - self.started, 1)

    def brief(self) -> dict:
        d = {"job": self.id, "tool": self.name, "state": self.state, "elapsed": self.elapsed}
        if self.args: d["args"] = _short(self.args)
        return d

    def full(self) -> dict:
        """brief plus the result, once there is one. A running job returns no
        result key at all rather than a null -- absent and empty are different
        answers, and the model acts on the difference."""
        d = self.brief()
        if self.call.done: d["result"] = self.call.result
        return d


class Registry:
    """Every job this process has started, newest last."""

    def __init__(self):
        self._lock = threading.Lock()
        self._jobs: dict[str, Job] = {}
        self._seq = itertools.count(1)

    def start(self, name: str, args: dict, fn) -> Job:
        """Launch fn on its own thread and hand the job back at once. The
        model-initiated door."""
        return self.adopt(name, args, Call(fn).start())

    def adopt(self, name: str, args: dict, call: Call) -> Job:
        """Take over a call that is already running. The user-initiated door,
        and the reason Call carries its own box: the thread was started by the
        agent loop and keeps going without it."""
        with self._lock:
            job = Job(f"bg_{next(self._seq)}", name, args or {}, call, time.monotonic())
            self._jobs[job.id] = job
            self._trim()
        return job

    def _trim(self):
        """Caller holds the lock. Only finished jobs are ever dropped, and the
        oldest first -- a running job has a live thread behind it and forgetting
        it would lose the only handle on that thread."""
        done = [j for j in self._jobs.values() if j.call.done]
        for j in done[:max(0, len(done) - MAX_KEPT)]: self._jobs.pop(j.id, None)

    def get(self, jid: str) -> Job | None:
        with self._lock: return self._jobs.get(jid)

    def list(self, state: str = None) -> list[Job]:
        with self._lock: jobs = list(self._jobs.values())
        return [j for j in jobs if not state or state == "all" or j.state == state]

    def running(self) -> list[Job]:
        return [j for j in self.list() if j.state in ("running", "killing")]

    def kill(self, jid: str) -> bool:
        """Ask, do not force. The token is cooperative, so a tool that watches
        it stops and one that does not runs to completion and is ignored --
        there is no way to kill a Python thread, and pretending otherwise would
        be a lie in the tool's output."""
        job = self.get(jid)
        if not job or job.call.done: return False
        job.call.token.cancel()
        return True

    def kill_all(self):
        for j in self.running(): j.call.token.cancel()

    def newly_finished(self) -> list[Job]:
        """Jobs that ended since the last time anyone asked. Marking them here
        rather than on read is what keeps the between-rounds notice to one
        mention per job."""
        with self._lock:
            out = [j for j in self._jobs.values() if j.call.done and not j.reported]
            for j in out: j.reported = True
        return out


JOBS = Registry()
