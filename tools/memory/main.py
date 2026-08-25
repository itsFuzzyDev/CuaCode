"""The `memory` tool: read and write what survives the conversation.

The index is built into the tool's own description rather than pushed into the
system prompt, the same way the skill tool lists skills. Two reasons, and the
second is the one that matters: describe() is re-asked every turn by
refresh_dynamic(), so a memory written now is listed in the next breath -- and
the system prompt stays a fixed thing that providers can cache.
"""
import os

from handler.session import store
from integrations.memory import loader, naming

def _index() -> str:
    """In-scope memories, one line each. Never raises: a broken memory
    directory must not take the whole toolbox down with it."""
    try:
        lines = loader.index_lines()
        total = len(loader.all_memories())
    except Exception:
        return ""
    if not lines:
        return "\n\nNothing remembered yet." + (
            f" ({total} out of scope - `search` finds them.)" if total else "")
    out = "\n\nIn scope now:\n" + "\n".join(lines)
    if (rest := total - len(lines)) > 0:
        out += (f"\n\n{rest} other{'s are' if rest > 1 else ' is'} out of scope for this directory "
                f"or this app. `search` reaches them.")
    return out

def describe(body: str) -> str:
    return body + _index()

def _session_id(ctx) -> str:
    d = (ctx or {}).get("session_dir") if isinstance(ctx, dict) else None
    return os.path.basename(str(d)) if d else ""

def run(args: dict, ctx) -> dict:
    action = args.get("action")
    try:
        if action == "list":
            return {"memories": [m.brief() for m in loader.in_scope()],
                    "total": len(loader.all_memories())}

        if action == "load":
            if not args.get("name"): return {"error": "name required"}
            m = loader.get(args["name"])
            loader.touch(m.name)
            return {"memory": m.full()}

        if action == "session":
            if not args.get("id"): return {"error": "id required"}
            return store.transcript(args["id"])

        if action == "search":
            if not args.get("query"): return {"error": "query required"}
            return {"results": loader.search(args["query"])}

        if action == "write":
            m = loader.write(name=args.get("name", ""),
                             description=args.get("description", ""),
                             body=args.get("body", ""),
                             type=args.get("type") or "project",
                             scope=args.get("scope") or "global",
                             source=args.get("source") or "agent",
                             session=_session_id(ctx))
            return {"memory": m.brief(), "path": str(m.path), "written": True}

        if action == "delete":
            if not args.get("name"): return {"error": "name required"}
            return loader.delete(args["name"])

        if action == "rename_session":
            title = naming.clean(args.get("title", ""))
            if not title: return {"error": "title required"}
            sid = _session_id(ctx)
            if not sid: return {"error": "no session is open"}
            renamed = naming.rename(sid, title, source="agent")
            if not renamed: return {"error": "the user named this session; leaving it alone"}
            return {"session_id": sid, "title": title, "renamed": True}

        return {"error": f"unknown action: {action}"}
    except ValueError as e:
        return {"error": str(e)}
