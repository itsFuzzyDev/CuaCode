"""Fan-out for agents. Two shapes, and the difference between them matters.

parallel() is a barrier: it waits for everything before returning anything.
pipeline() is not: each item walks every stage on its own, so item A can be in
stage three while item B is still in stage one, and the wall clock is the
slowest single chain rather than the sum of the slowest thing in each stage.

Pipeline is the one you want almost always. A barrier is only right when a
stage genuinely needs every result from the one before it at once -- dedup
across the whole set, an early exit on a total count, a synthesis that
compares findings to each other. "I need to flatten the list first" is not
that; flatten inside a stage.

Failures come back as None rather than as exceptions. One agent hitting a rate
limit should cost its own result and nothing else, and a caller filtering
None out of a list is a great deal simpler than a caller unwinding a fan-out
halfway through.
"""
import os
from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context

# Concurrent model requests, not cpu work -- these threads are asleep on a
# socket almost the whole time. The cap is about not opening thirty billable
# streams because a list happened to be long.
MAX_WORKERS = min(8, max(2, (os.cpu_count() or 4) - 2))

def _safe(fn, on_error):
    try: return fn()
    except Exception as e:
        if on_error:
            try: on_error(e)
            except Exception: pass
        return None

def parallel(thunks, on_error=None) -> list:
    """Run every thunk, return results in the order given. Barrier.

    Takes zero-argument callables rather than (fn, args) pairs so a caller
    binds with a lambda and this never has to know anything about arguments.
    """
    thunks = list(thunks)
    if not thunks: return []
    if len(thunks) == 1: return [_safe(thunks[0], on_error)]
    with ThreadPoolExecutor(max_workers=min(MAX_WORKERS, len(thunks))) as ex:
        # A fresh context copy per submit, taken here on the calling thread. A
        # worker thread otherwise starts from the ContextVar defaults, which
        # would reset the nesting depth to zero and quietly un-bound recursion.
        # One copy per thunk, because a Context cannot be entered twice at once.
        futures = [ex.submit(copy_context().run, _safe, t, on_error) for t in thunks]
        return [f.result() for f in futures]

def pipeline(items, *stages, on_error=None) -> list:
    """Every item through every stage, independently. No barrier between stages.

    Each stage is called (previous_result, original_item, index) -- the later
    two so a stage three deep can still label its work without stage one having
    to thread the original through its return value. The first stage sees the
    item as both of its first two arguments.

    A stage that raises drops that item to None and skips its remaining stages;
    the other items carry on.
    """
    items = list(items)
    def chain(index, item):
        value = item
        for stage in stages:
            value = stage(value, item, index)
        return value
    # Default arguments, not closure capture: a lambda closing over the loop
    # variable would see whatever it ended on by the time a thread ran it.
    return parallel([lambda i=i, it=it: chain(i, it) for i, it in enumerate(items)], on_error)
