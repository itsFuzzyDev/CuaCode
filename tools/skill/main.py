"""The `skill` tool: load one skill's instructions.

Which skills exist is read off disk at load time and again on every turn, so a
skill written during this conversation is loadable in the next breath.
"""
from integrations.skills import loader as skills

def _installed() -> dict:
    try: return skills.load_skills()
    except Exception: return {}

def describe(body: str) -> str:
    found = _installed()
    if not found:
        return body + "\n\nNo skills are installed."
    lines = "\n".join(f"- {name}: {s.description or 'no description'}"
                      for name, s in sorted(found.items()))
    return f"{body}\n\nAvailable skills:\n{lines}"

def schema() -> dict:
    names = sorted(_installed())
    which = {"type": "string", "description": "Which skill to load."}
    if names: which["enum"] = names
    return {"properties": {"skill": which}, "required": ["skill"]}

def run(args: dict, ctx) -> dict:
    try: s = skills.get(args["skill"])
    except ValueError as e: return {"error": str(e)}
    out = {"name": s.name, "instructions": s.body, "dir": str(s.path)}
    if s.files: out["files"] = s.files
    return out
