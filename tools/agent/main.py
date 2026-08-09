"""The `agent` tool: run one installed subagent.

Which subagents exist is a question about this machine, answered at load time
rather than by whatever the InputSchema.json on disk happened to say. That is
what describe() and schema() are for -- the loader calls them if a tool defines
them, so the model is offered the agents actually installed and an enum it
cannot spell wrong.
"""
from integrations.subagents import loader as agents
from handler.agent import subagent

def _installed() -> dict:
    # refresh: this is called once per turn, and the point of calling it that
    # often is to notice a subagent file written a moment ago. Two directory
    # globs is cheaper than the round trip that finds out it was missed.
    try: return agents.load_agents(refresh=True)
    except Exception: return {}

def describe(body: str) -> str:
    found = _installed()
    if not found:
        return body + "\n\nNo subagents are installed, so this tool has nothing to run."
    lines = "\n".join(f"- {name}: {spec.description or 'no description'}"
                      for name, spec in sorted(found.items()))
    return f"{body}\n\nInstalled subagents:\n{lines}"

def schema() -> dict:
    found = sorted(_installed())
    agent = {"type": "string", "description": "Which subagent to run."}
    # An empty enum is not a constraint any provider accepts, and the tool is
    # unusable in that state anyway -- describe() has already said so.
    if found: agent["enum"] = found
    return {"properties": {
                "agent": agent,
                "prompt": {"type": "string",
                           "description": "The whole job, self-contained. The subagent sees nothing else."}},
            "required": ["agent", "prompt"]}

def run(args: dict, ctx) -> dict:
    # Depth is enforced in subagent.run() as well; refusing here means the
    # reason comes back in words instead of as an empty result.
    if subagent.depth() > subagent.MAX_DEPTH:
        return {"error": "subagents cannot start further subagents"}
    try: spec = agents.get(args["agent"])
    except ValueError as e: return {"error": str(e)}
    return subagent.run(spec, args["prompt"], ctx=ctx)
