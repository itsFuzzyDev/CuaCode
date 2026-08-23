"""Skills: instructions loaded on demand instead of carried all the time.

A skill is a folder with a SKILL.md in it. Only the name and one-line
description are ever in the model's context by default -- enough to decide the
skill is relevant -- and the body is read when it asks for it. That is the
whole trick: fifty skills cost fifty lines of context, not fifty documents.

Anything else in the folder (templates, examples, scripts) stays on disk. The
body can point at those by path and the agent reads them with the file tool if
it needs them.

One skill in ten is not like that. A house style, a safety rule, a format every
answer has to be in -- something that is only useful if it is already in force
by the time the agent decides anything. Asking for it in AGENTS.md ("always load
the X skill") does not work reliably: the agent has to notice, decide, and spend
a tool call, and it is exactly on the turn it forgets that the rule mattered. So
those are marked `always: true` and their bodies go into the system prompt at
startup, whole, every conversation. That costs their full length on every
request -- which is the trade, made deliberately, for a handful of skills rather
than for fifty.
"""
from dataclasses import dataclass
from pathlib import Path

from tools.loader import parse_frontmatter
from handler.session import store

_BUNDLED = Path(__file__).parent
MAX_BODY = 20_000        # a skill that does not fit is a skill that should link out

# What every always-on skill may cost between them, in characters. A ceiling
# rather than a limit on how many may be marked: forcing six skills on is a
# decision, forcing the window shut is an accident. Bodies past it are dropped
# and named in the block, so the reason is in front of the model rather than
# only in a log nobody reads.
MAX_ALWAYS = 40_000

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
    # In the system prompt from startup rather than loaded on demand. Set by
    # `always: true` in the skill's own frontmatter, or by naming it in the
    # config's always_skills, which is how a bundled skill is forced on without
    # editing a file the next update overwrites.
    always: bool = False

def _flag(meta: dict, *names) -> bool:
    """A frontmatter bool, written with dashes or underscores, either way."""
    for n in names:
        if n in meta: return str(meta[n]).strip().lower() in ("true", "yes", "1", "on")
    return False

def always_names() -> set:
    """Skill names the config forces on, whatever their frontmatter says.

    Imported here rather than at module scope: config reaches into the provider
    registry, and skills are loaded from inside it while /context is counting
    them. A missing or malformed key is no skills forced, never an error -- this
    is read on the way to building the system prompt.
    """
    try:
        from handler import config
        names = config.load().get("always_skills") or []
        return {str(n).strip() for n in names if str(n).strip()}
    except Exception:
        return set()


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
                 always=_flag(meta, "always", "always-load", "always_load",
                              "auto-load", "auto_load"),
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

    An always-on skill is in neither scope. Its body is already in the system
    prompt, so offering it to the skill tool or the palette would only be a way
    of paying for it twice; `load_skills()` with no scope still has it, which is
    what the system prompt and the readout are built from.
    """
    out = {}
    for d in (_BUNDLED, user_dir()):          # user second: it overwrites
        if not d.exists(): continue
        for folder in sorted(p for p in d.iterdir() if p.is_dir()):
            if folder.name.startswith(("_", ".")): continue
            try: s = _skill(folder)
            except Exception: continue        # one broken skill must not hide the rest
            if not s: continue
            out[s.name] = s
    for name in always_names() & set(out):
        out[name].always = True
    if scope in ("model", "user"):
        out = {n: s for n, s in out.items()
               if not s.always and (s.model_ok if scope == "model" else s.user_ok)}
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


# --------------------------------------------------------------------------
# always on: in the system prompt from startup

def always_on() -> list:
    """The skills marked always-on, in name order."""
    return [s for _, s in sorted(load_skills().items()) if s.always]


def always_block() -> str:
    """Their bodies as one system segment, or "" when none is marked.

    Built the same way on every turn from the same files, so a provider caching
    the system prompt as a prefix keeps hitting it; an edit to a skill lands on
    the next turn, which costs one cache miss and is worth it.

    Whole bodies, not the index -- that is the entire point of the flag. The
    budget is spent in name order and what does not fit is named rather than
    silently dropped, because a skill that is quietly not in force is worse than
    one that is obviously not.
    """
    loaded, dropped, used = [], [], 0
    for s in always_on():
        if used + len(s.body) > MAX_ALWAYS:
            dropped.append(s.name)
            continue
        used += len(s.body)
        loaded.append(f"<skill name=\"{s.name}\" dir=\"{s.path}\">\n{s.body}\n"
                      + (f"\nFiles next to it, on disk, unread: {', '.join(s.files)}\n" if s.files else "")
                      + "</skill>")
    if not loaded and not dropped: return ""
    cut = (f"\n[not loaded, no room left under {MAX_ALWAYS} characters: "
           f"{', '.join(dropped)}. Unmark one of the others or move detail into "
           "files beside it.]\n") if dropped else ""
    return ("<always_on_skills>\n"
            "These skills were marked always-on, so their instructions are here in full\n"
            "rather than waiting behind the skill tool. They are in force for every turn\n"
            "of this conversation without being asked for. Do not load them again -- they\n"
            "are not in the skill tool's list, and this is the same text it would return.\n\n"
            + "\n\n".join(loaded) + cut + "\n</always_on_skills>") if loaded else (
            "<always_on_skills>" + cut + "</always_on_skills>")
