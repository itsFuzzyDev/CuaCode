"""The `workflow` tool: run one stored orchestration script.

Same runtime-description trick as the agent tool -- what is installed is read
off disk at load time, so the model is offered the workflows that exist rather
than a list written when this file was.
"""
from handler.agent import workflow as wf, subagent

def _listing() -> list:
    try: return wf.listing()
    except Exception: return []

def describe(body: str) -> str:
    found = _listing()
    if not found:
        return body + "\n\nNo workflows are installed, so this tool has nothing to run."
    lines = "\n".join(f"- {name}: {desc}" for name, desc in sorted(found))
    return f"{body}\n\nInstalled workflows:\n{lines}"

def schema() -> dict:
    names = sorted(name for name, _ in _listing())
    which = {"type": "string", "description": "Which stored workflow to run."}
    if names: which["enum"] = names
    return {"properties": {
                "workflow": which,
                "args": {"type": "object",
                         "description": "Input for the workflow, in whatever shape its description asks for."}},
            "required": ["workflow"]}

def run(args: dict, ctx) -> dict:
    # A workflow spawns agents, so a subagent running one would be recursion
    # with a for-loop attached.
    if subagent.depth() > 0:
        return {"error": "subagents cannot run workflows"}
    return wf.run(args["workflow"], args.get("args") or {}, ctx=ctx)
