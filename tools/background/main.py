"""Reading the background registry from inside the agent loop.

Everything here is a view. The jobs themselves are started by the loop, not by
this tool, because backgrounding is something the loop does to a call rather
than something a call can do to itself -- and because half of them are started
by the user, mid-call, with no model involvement at all.
"""
import time

from handler.agent.background import JOBS

DEFAULT_WAIT = 30
MAX_WAIT = 300
POLL = 0.1


def _wait(job, timeout: float, cancel) -> dict:
    """Sit on one job until it ends.

    Polled rather than joined, so the cancel in ctx is read on the way round.
    Without that the whole point is undone: this call runs on the interruptible
    thread like every other, but a thread parked in join() is exactly the thing
    that thread exists to avoid becoming.
    """
    deadline = time.monotonic() + timeout
    while not job.call.done:
        if cancel is not None and cancel.cancelled():
            return {**job.brief(), "waited": True, "cancelled": True}
        if time.monotonic() >= deadline:
            return {**job.brief(), "waited": True, "timeout": True,
                    "note": f"still running after {timeout}s -- wait again or leave it"}
        time.sleep(POLL)
    return job.full()


def run(args: dict, ctx) -> dict:
    action = (args.get("action") or "").strip()
    cancel = ctx.get("cancel") if hasattr(ctx, "get") else None

    if action == "list":
        jobs = JOBS.list(args.get("state") or "all")
        return {"jobs": [j.brief() for j in jobs], "count": len(jobs)}

    jid = (args.get("job") or "").strip()
    if not jid: return {"error": f"job id required for {action}"}
    job = JOBS.get(jid)
    if not job:
        known = [j.id for j in JOBS.list()]
        return {"error": f"no job {jid!r}" + (f" (have {known})" if known else " -- nothing has run in the background")}

    if action == "output":
        return job.full()

    if action == "kill":
        if job.call.done:
            return {**job.brief(), "note": "already finished -- nothing to stop, read it with output"}
        JOBS.kill(jid)
        # A moment's grace, then report what is actually true rather than what
        # was asked for. The token is cooperative and some tools do not watch
        # it, and "killed" over a process still running is the one answer that
        # would send the model off believing something false.
        deadline = time.monotonic() + 2.0
        while not job.call.done and time.monotonic() < deadline: time.sleep(POLL)
        return {**job.brief(),
                "note": "stopped" if job.call.done else
                        "asked to stop, but this tool does not check -- it will run to the end "
                        "and its result will be discarded"}

    if action == "wait":
        timeout = max(1.0, min(float(args.get("timeout") or DEFAULT_WAIT), MAX_WAIT))
        return _wait(job, timeout, cancel)

    return {"error": f"unknown action: {action!r}"}
