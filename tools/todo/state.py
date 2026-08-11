"""The list itself, kept apart from the tool that edits it.

Two callers need it and only one of them is the tool. The agent loop reads a
summary to decide whether the list has gone stale, and importing a tool's
handler to ask that would drag the whole tool in; a module with no dependencies
but the standard library can be imported from either side.

One list per conversation, in the session's own directory. A global list -- what
this replaced -- was shared by every chat ever held, so yesterday's half-finished
plan was still sitting there this morning claiming to be what the agent was doing.
Scoping it to the session is what makes "look back at the list" mean "look back at
*this* task": it appears with the conversation, survives a reload, and goes when
the conversation does.
"""
import json, os
from datetime import datetime, timezone
from pathlib import Path

PENDING, ACTIVE, DONE, DROPPED = "pending", "in_progress", "done", "dropped"
STATES = (PENDING, ACTIVE, DONE, DROPPED)
OPEN = (PENDING, ACTIVE)

# A plan longer than this is not a plan, it is the whole task written out. The
# cap is deliberately generous -- it exists to catch a model looping on `add`,
# not to argue with a genuinely long job.
MAX_ITEMS = 60
MAX_TEXT = 200
MAX_NOTE = 500

FILE = "todo.json"

# Lists with nowhere to live: a subagent's, or a run with no session directory at
# all. Held in the process and never written, because both are gone by the time
# anyone could read the file -- and a subagent writing into its parent's session
# directory would overwrite the plan the parent is working through.
_memory: dict[str, dict] = {}


def _empty() -> dict:
    return {"items": [], "next_id": 1, "updated": ""}


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _depth() -> int:
    """How many agents deep this call is; 0 in the main loop."""
    try:
        from handler.agent import subagent
        return subagent.depth()
    except Exception:
        return 0


def _dir(ctx) -> str:
    if ctx is None: return ""
    d = ctx.get("session_dir") if isinstance(ctx, dict) else getattr(ctx, "session_dir", None)
    return str(d or "")


def path(ctx) -> Path | None:
    """Where this run's list lives, or None for one that lives in memory."""
    if _depth() > 0: return None
    d = _dir(ctx)
    return Path(d) / FILE if d else None


def _slot(ctx) -> str:
    return f"{_depth()}:{_dir(ctx)}"


def load(ctx) -> dict:
    p = path(ctx)
    if p is None: return _memory.setdefault(_slot(ctx), _empty())
    try:
        data = json.loads(p.read_text())
    except (OSError, ValueError):
        # A missing file is the normal case -- most conversations never plan
        # anything -- and an unreadable one is not worth failing a tool call
        # over when the recovery is an empty list the agent can just refill.
        return _empty()
    if not isinstance(data, dict) or not isinstance(data.get("items"), list): return _empty()
    return data


def save(ctx, data: dict) -> dict:
    data["updated"] = now()
    p = path(ctx)
    if p is None:
        _memory[_slot(ctx)] = data
        return data
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        # Whole-file rewrite through a temp file: the list is tens of items, and
        # a half-written todo.json read back next turn would be worse than a lost
        # update.
        tmp = p.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, indent=1))
        os.replace(tmp, p)
    except OSError:
        _memory[_slot(ctx)] = data
    return data


def add(data: dict, text: str) -> dict:
    item = {"id": data["next_id"], "text": text[:MAX_TEXT], "state": PENDING,
            "note": "", "added": now(), "closed": ""}
    data["items"].append(item)
    data["next_id"] += 1
    return item


def find(data: dict, tid) -> dict | None:
    try: tid = int(tid)
    except (TypeError, ValueError): return None
    return next((i for i in data["items"] if i["id"] == tid), None)


def active(data: dict) -> dict | None:
    return next((i for i in data["items"] if i["state"] == ACTIVE), None)


def upcoming(data: dict) -> dict | None:
    return next((i for i in data["items"] if i["state"] == PENDING), None)


def _brief(item: dict) -> dict:
    out = {"id": item["id"], "text": item["text"], "state": item["state"]}
    if item.get("note"): out["note"] = item["note"]
    return out


def view(data: dict, **extra) -> dict:
    """The whole list, every time.

    Returning it on writes as well as reads is the point: the agent's most recent
    tool result then always holds the current state of the plan, so remembering
    what it was doing costs nothing and needs no extra call. A write that echoed
    only the row it touched would make the list something you have to go and ask
    about, which is the habit this tool exists to remove.
    """
    items = data["items"]
    done = sum(1 for i in items if i["state"] == DONE)
    out = {"todo": [_brief(i) for i in items],
           "summary": f"{done}/{len(items)} done" if items else "empty",
           "open": sum(1 for i in items if i["state"] in OPEN)}
    if (cur := active(data)): out["current"] = _brief(cur)
    if (nxt := upcoming(data)): out["next"] = _brief(nxt)
    return {**out, **extra}


def snapshot(ctx) -> dict | None:
    """What the loop needs to decide whether to nudge, or None for no list.

    Deliberately cheap and deliberately silent: it runs every round, and a
    conversation that never planned anything must pay a stat call for it and
    nothing more.
    """
    try:
        data = load(ctx)
    except Exception:
        return None
    open_items = [i for i in data["items"] if i["state"] in OPEN]
    if not open_items: return None
    cur, nxt = active(data), upcoming(data)
    done = sum(1 for i in data["items"] if i["state"] == DONE)
    return {"open": len(open_items), "total": len(data["items"]), "done": done,
            "current": cur["text"] if cur else "", "next": nxt["text"] if nxt else ""}
