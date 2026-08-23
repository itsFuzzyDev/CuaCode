"""Agents that live in files.

Same shape as an AgentSpec built inline, same shape as a tool's
Description.md: yaml frontmatter for the machine-readable half, body for the
system prompt. Nothing here does any running -- it reads files into specs and
hands them to subagent.run(), which does not care where a spec came from.

Two directories. The ones shipped with the app live next to this file; the
ones you write live in ~/.cuacode/subagents, and a user file wins on a name
collision so a bundled agent can be overridden without editing the repo.
"""
from pathlib import Path

from tools.loader import parse_frontmatter
from handler.agent.subagent import AgentSpec
from handler.session import store

_BUNDLED = Path(__file__).parent
_cache = None

def user_dir() -> Path:
    d = store.home() / "subagents"
    d.mkdir(parents=True, exist_ok=True)
    return d

def _spec(path: Path) -> AgentSpec:
    meta, body = parse_frontmatter(path.read_text())
    tools = meta.get("tools")
    if isinstance(tools, str):
        # "*" is every tool, which is not the same as the empty list, which is
        # deliberately none. A bare string otherwise means one tool name.
        tools = None if tools.strip() == "*" else [tools]
    return AgentSpec(
        name=meta.get("name") or path.stem,
        description=meta.get("description", ""),
        system=body,
        # None means unrestricted; the key being absent means the author did
        # not think about it, and an agent nobody scoped gets no tools rather
        # than all of them.
        tools=tools if tools is not None or "tools" not in meta else None,
        schema=meta.get("output"),
        provider=meta.get("provider"),
        model=meta.get("model"),
        # Absent means inherit the conversation's level, which is what a file
        # that never mentions effort is asking for. A file that does name one
        # is taken at its word -- see subagent._effort.
        effort=meta.get("effort", ""),
        max_rounds=int(meta.get("max_rounds", 8)),
        params=meta.get("params"))

def load_agents(refresh: bool = False) -> dict:
    """{name: AgentSpec}. Cached, because this is read on every tool-registry
    build; pass refresh to pick up a file you just wrote."""
    global _cache
    if _cache is not None and not refresh: return _cache
    out = {}
    for d in (_BUNDLED, user_dir()):          # user second: it overwrites
        for f in sorted(d.glob("*.md")):
            try: spec = _spec(f)
            except Exception: continue        # one malformed file must not hide the rest
            out[spec.name] = spec
    _cache = out
    return out

def get(name: str) -> AgentSpec:
    agents = load_agents()
    if name not in agents:
        raise ValueError(f"unknown agent: {name!r} (have {sorted(agents) or 'none installed'})")
    return agents[name]
