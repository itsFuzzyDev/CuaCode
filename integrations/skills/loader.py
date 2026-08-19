"""Skills: instructions loaded on demand instead of carried all the time.

A skill is a folder with a SKILL.md in it. Only the name and one-line
description are ever in the model's context by default -- enough to decide the
skill is relevant -- and the body is read when it asks for it. That is the
whole trick: fifty skills cost fifty lines of context, not fifty documents.

Anything else in the folder (templates, examples, scripts) stays on disk. The
body can point at those by path and the agent reads them with the file tool if
it needs them.
"""
from dataclasses import dataclass
from pathlib import Path

from tools.loader import parse_frontmatter
from handler.session import store

_BUNDLED = Path(__file__).parent
MAX_BODY = 20_000        # a skill that does not fit is a skill that should link out

@dataclass
class Skill:
    name: str
    description: str
    path: Path
    body: str
    files: list
    # Who is allowed to reach for it. A skill is offered to both by default;
    # `disable-model-invocation: true` keeps it out of the skill tool's list,
    # `disable-user-invocation: true` keeps it out of the frontend's /palette.
    # Turning both off would leave a skill nothing can load, so that case is
    # dropped at load time rather than kept as a skill nobody can have.
    model_ok: bool = True
    user_ok: bool = True

def _flag(meta: dict, *names) -> bool:
    """A frontmatter bool, written with dashes or underscores, either way."""
    for n in names:
        if n in meta: return str(meta[n]).strip().lower() in ("true", "yes", "1", "on")
    return False

def user_dir() -> Path:
    d = store.home() / "skills"
    d.mkdir(parents=True, exist_ok=True)
    return d

def _skill(folder: Path):
    f = folder / "SKILL.md"
    if not f.exists(): return None
    meta, body = parse_frontmatter(f.read_text())
    if len(body) > MAX_BODY:
        body = body[:MAX_BODY] + f"\n\n[truncated: SKILL.md is over {MAX_BODY} characters]"
    model_ok = not _flag(meta, "disable-model-invocation", "disable_model_invocation")
    user_ok = not _flag(meta, "disable-user-invocation", "disable_user_invocation")
    if not model_ok and not user_ok:
        return None                       # nothing could ever load it; do not pretend it is installed
    return Skill(model_ok=model_ok, user_ok=user_ok,
                 name=meta.get("name") or folder.name,
                 description=meta.get("description", ""),
                 path=folder, body=body,
                 # Listed, not read. What is worth loading is the skill's own
                 # decision, and it says so in the body.
                 files=sorted(str(p.relative_to(folder)) for p in folder.rglob("*")
                              if p.is_file() and p.name != "SKILL.md"))

def load_skills(refresh: bool = True, scope: str = None) -> dict:
    """Every installed skill, or only the ones `scope` is allowed to load.

    scope is "model" (the skill tool), "user" (a frontend's slash palette), or
    None for everything -- counting them for /context does not care who may
    invoke what.
    """
    out = {}
    for d in (_BUNDLED, user_dir()):          # user second: it overwrites
        if not d.exists(): continue
        for folder in sorted(p for p in d.iterdir() if p.is_dir()):
            if folder.name.startswith(("_", ".")): continue
            try: s = _skill(folder)
            except Exception: continue        # one broken skill must not hide the rest
            if not s: continue
            if scope == "model" and not s.model_ok: continue
            if scope == "user" and not s.user_ok: continue
            out[s.name] = s
    return out

def get(name: str, scope: str = None) -> Skill:
    found = load_skills(scope=scope)
    if name not in found:
        raise ValueError(f"unknown skill: {name!r} (have {sorted(found) or 'none installed'})")
    return found[name]

# --------------------------------------------------------------------------
# user invocation: /<name> typed in a frontend

def listing(scope: str = "user") -> list:
    """Name and description of every skill `scope` may load, for a picker."""
    return [{"name": s.name, "description": s.description}
            for _, s in sorted(load_skills(scope=scope).items())]

def invocation(text: str) -> str:
    """The block for a user message that opens with `/<skill>`, or "".

    The user's own line is left alone -- it is what they typed, and the records
    are what the conversation was -- so the instructions ride alongside it the
    way recall and project docs do. Anything typed after the name is the user's
    task, already in the message, so it is not repeated here.
    """
    line = (text or "").lstrip()
    if not line.startswith("/"): return ""
    name = line[1:].split(None, 1)[0].strip() if len(line) > 1 else ""
    if not name: return ""
    try: s = get(name, scope="user")
    except ValueError: return ""
    return (f"skill loaded: {s.name}\n\n"
            f"<skill name=\"{s.name}\" dir=\"{s.path}\">\n"
            "The user asked for this skill by name. These are its instructions;\n"
            "follow them for the message it came with.\n\n"
            f"{s.body}\n"
            + (f"\nFiles next to it, on disk, unread: {', '.join(s.files)}\n" if s.files else "")
            + "</skill>")
