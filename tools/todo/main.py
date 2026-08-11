"""The `todo` tool: plan a job, then work the plan.

What this replaces was a shared SQLite table of strings. The difference is not
the storage, it is that a list is only useful if it says what is *happening* --
which step is running, what the last one found, what is left -- and a checklist
of descriptions says none of that.

So the actions are shaped around the loop the agent actually runs: `plan` writes
the steps out before any of them starts, `start` says which one it is on, `done`
closes it and hands back the next, `note` records what a step turned up so the
list is still worth reading twenty rounds later. Every one of them returns the
whole list, which is what makes looking back free.
"""
from tools.todo import state


def _steps(args: dict) -> tuple[list, str]:
    """The steps out of `steps`, or out of `task` for a single one."""
    raw = args.get("steps")
    if isinstance(raw, str): raw = [raw]
    if raw is None and args.get("task"): raw = [args["task"]]
    out = [str(s).strip() for s in (raw or []) if str(s).strip()]
    if not out: return [], "steps required -- a list of short imperative lines"
    return out, ""


def _note(args: dict) -> str:
    return str(args.get("note") or "").strip()[:state.MAX_NOTE]


def run(args: dict, ctx) -> dict:
    action = args.get("action")
    data = state.load(ctx)

    if action == "list":
        return state.view(data)

    if action == "clear":
        n = len(data["items"])
        # next_id is not reset: ids from the plan just abandoned are all over
        # the conversation above, and reusing them would make the transcript lie.
        data["items"] = []
        state.save(ctx, data)
        return state.view(data, cleared=n)

    if action in ("plan", "add"):
        steps, err = _steps(args)
        if err: return {"error": err}
        if action == "plan":
            # A new plan replaces the old one outright. Merging would leave the
            # agent working two plans at once, and the moment it writes a fresh
            # one is exactly the moment the previous one stopped being the job.
            data["items"] = []
        room = state.MAX_ITEMS - len(data["items"])
        if room <= 0:
            return {"error": f"list is full at {state.MAX_ITEMS} items -- close some, or clear and re-plan"}
        added = [state.add(data, s) for s in steps[:room]]
        state.save(ctx, data)
        extra = {"added": [i["id"] for i in added]}
        if len(steps) > room: extra["ignored"] = len(steps) - room
        return state.view(data, **extra)

    item = state.find(data, args.get("id"))
    if action in ("start", "done", "drop", "note") and item is None:
        return {"error": f"no todo with id {args.get('id')!r} -- call list to see them"}

    if action == "start":
        if item["state"] == state.DONE:
            return {"error": f"todo {item['id']} is already done"}
        warning = ""
        # One in_progress at a time, enforced rather than suggested: two of them
        # means the list has stopped describing what is happening, which is the
        # only thing it is for. The interrupted step goes back to pending and is
        # said out loud, so nothing is silently abandoned.
        if (prev := state.active(data)) and prev["id"] != item["id"]:
            prev["state"] = state.PENDING
            warning = f"todo {prev['id']} was in progress and is back to pending: {prev['text']}"
        item["state"] = state.ACTIVE
        if n := _note(args): item["note"] = n
        state.save(ctx, data)
        return state.view(data, **({"warning": warning} if warning else {}))

    if action in ("done", "drop"):
        item["state"] = state.DONE if action == "done" else state.DROPPED
        item["closed"] = state.now()
        if n := _note(args): item["note"] = n
        state.save(ctx, data)
        out = state.view(data)
        # Said in words as well as in the list, because this is the moment the
        # agent decides what to do next and the answer should not need reading
        # off an array.
        if out.get("next"): out["do_next"] = out["next"]["text"]
        elif not out["open"]: out["do_next"] = "nothing left open -- report what was done"
        return out

    if action == "note":
        n = _note(args)
        if not n: return {"error": "note required"}
        item["note"] = n
        state.save(ctx, data)
        return state.view(data)

    return {"error": f"unknown action: {action}"}
