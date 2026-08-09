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
    return Skill(name=meta.get("name") or folder.name,
                 description=meta.get("description", ""),
                 path=folder, body=body,
                 # Listed, not read. What is worth loading is the skill's own
                 # decision, and it says so in the body.
                 files=sorted(str(p.relative_to(folder)) for p in folder.rglob("*")
                              if p.is_file() and p.name != "SKILL.md"))

def load_skills(refresh: bool = True) -> dict:
    out = {}
    for d in (_BUNDLED, user_dir()):          # user second: it overwrites
        if not d.exists(): continue
        for folder in sorted(p for p in d.iterdir() if p.is_dir()):
            if folder.name.startswith(("_", ".")): continue
            try: s = _skill(folder)
            except Exception: continue        # one broken skill must not hide the rest
            if s: out[s.name] = s
    return out

def get(name: str) -> Skill:
    found = load_skills()
    if name not in found:
        raise ValueError(f"unknown skill: {name!r} (have {sorted(found) or 'none installed'})")
    return found[name]
