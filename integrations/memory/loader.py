"""Memory: the things that stay true after the conversation ends.

Same bargain as skills, for the same reason -- an index in context, bodies on
demand. A memory is one file holding one fact, and only its name and one line
are ever carried by default. Two hundred memories cost two hundred lines, not
two hundred documents.

Files rather than a database, and one fact per file rather than a list, because
both halves of that are what make a memory editable by hand and diffable when
it turns out to be wrong. A memory the user cannot open and fix is a memory
they have to delete.

Scope is what keeps the index small as the corpus grows. A memory about one
project is noise everywhere else, and a memory about an app's dialogs is noise
until that app is on screen, so nothing is global unless it is actually global.
"""
import hashlib, os, re, yaml
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from tools.loader import parse_frontmatter
from handler.session import store

# A memory that does not fit in this is not a memory, it is a document. Write
# the document and point at it -- a `reference` memory holding a path costs one
# line of index and the file tool reads the rest when it is wanted.
MAX_BODY = 4000
MAX_DESC = 200

NAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{1,63}$")
SCOPE_RE = re.compile(r"^(global|(projects|apps)/[a-z0-9][a-z0-9._-]{0,63})$")

TYPES = ("user", "feedback", "project", "app", "reference")
SOURCES = ("user", "agent", "external")

# Where the current turn is happening. Set once per turn by the loop, because
# the two callers that need it -- the tool's own description and the recall
# block -- are both called from places that never see a ctx. Falls back to the
# worker's own cwd, which is right often enough and wrong harmlessly: the worst
# case is an index scoped to the wrong project, not a path escape.
_CWD = ""

def set_cwd(path: str):
    global _CWD
    _CWD = str(path or "")

def cwd() -> str:
    return _CWD or os.getcwd()

def root() -> Path:
    d = store.home() / "memory"
    d.mkdir(parents=True, exist_ok=True)
    return d

def archive_dir() -> Path:
    d = root() / ".archive"
    d.mkdir(parents=True, exist_ok=True)
    return d

def project_slug(path: str = None) -> str:
    """A directory's scope name: readable half, unique half.

    The basename alone collides -- everyone has three checkouts called `core` --
    and the hash alone is unreadable in a listing, which matters because these
    are directory names the user is expected to browse.
    """
    p = os.path.abspath(os.path.expanduser(path or cwd()))
    base = re.sub(r"[^a-z0-9]+", "-", os.path.basename(p).lower()).strip("-") or "root"
    return f"{base[:32]}-{hashlib.sha1(p.encode()).hexdigest()[:6]}"

def scope_for(path: str = None) -> str:
    return f"projects/{project_slug(path)}"

def _safe_scope(scope: str) -> str:
    scope = (scope or "global").strip().strip("/")
    if scope in ("project", "projects"): scope = scope_for()
    if not SCOPE_RE.match(scope): raise ValueError(f"bad scope: {scope!r}")
    return scope

def _safe_name(name: str) -> str:
    name = (name or "").strip().lower()
    if not NAME_RE.match(name):
        raise ValueError(f"bad memory name: {name!r} (lowercase, digits, - . _, 2-64 chars)")
    return name

@dataclass
class Memory:
    name: str
    description: str
    type: str
    scope: str
    source: str
    body: str
    path: Path
    created: str = ""
    updated: str = ""
    session: str = ""
    uses: int = 0
    used: str = ""

    def brief(self) -> dict:
        return {"name": self.name, "description": self.description, "type": self.type,
                "scope": self.scope, "source": self.source, "updated": self.updated}

    def full(self) -> dict:
        return {**self.brief(), "body": self.body, "path": str(self.path),
                "created": self.created, "uses": self.uses}

def _read(p: Path) -> Memory | None:
    try: meta, body = parse_frontmatter(p.read_text())
    except Exception: return None
    if not isinstance(meta, dict): return None
    name = str(meta.get("name") or p.stem)
    if not NAME_RE.match(name): return None
    scope = str(meta.get("scope") or "global")
    return Memory(name=name, description=str(meta.get("description") or "")[:MAX_DESC],
                  type=str(meta.get("type") or "project"), scope=scope,
                  source=str(meta.get("source") or "agent"), body=body, path=p,
                  created=str(meta.get("created") or ""), updated=str(meta.get("updated") or ""),
                  session=str(meta.get("session") or ""),
                  uses=int(meta.get("uses") or 0), used=str(meta.get("used") or ""))

def all_memories() -> dict[str, Memory]:
    """Every memory on disk, by name. One flat namespace across scopes: two
    memories that share a name are one memory that was written twice, and the
    index has no room to disambiguate them anyway."""
    out = {}
    r = root()
    for p in sorted(r.rglob("*.md")):
        if p.name == "MEMORY.md": continue
        if any(part.startswith(".") for part in p.relative_to(r).parts): continue
        m = _read(p)
        if m: out[m.name] = m
    return out

def in_scope(path: str = None, apps: list = None) -> list[Memory]:
    """What is worth putting in front of the model right now.

    Global always, this project always, an app's memories only when that app is
    named. Everything else stays on disk and is still reachable through search
    -- out of scope means unlisted, never unavailable.
    """
    want = {"global", scope_for(path)}
    # What is frontmost arrives as whatever the platform calls it -- "Safari",
    # "com.apple.Safari", "Google Chrome" -- and the scope was named by hand.
    # Matched on the pieces so the two do not have to have agreed in advance.
    here = {p for a in (apps or []) if a
            for p in re.sub(r"[^a-z0-9]+", "-", str(a).lower()).strip("-").split("-") if p}
    out = []
    for m in all_memories().values():
        if m.scope in want:
            out.append(m)
        elif m.scope.startswith("apps/"):
            app = m.scope.split("/", 1)[1]
            if app in here or any(part in here for part in app.split("-")): out.append(m)
    return sorted(out, key=lambda m: (m.scope != "global", m.name))

def get(name: str) -> Memory:
    found = all_memories()
    name = (name or "").strip().lower()
    if name not in found:
        raise ValueError(f"no memory named {name!r}")
    return found[name]

def _path_for(name: str, scope: str) -> Path:
    d = root() / scope
    d.mkdir(parents=True, exist_ok=True)
    return d / f"{name}.md"

def _render(meta: dict, body: str) -> str:
    return "---\n" + yaml.safe_dump(meta, sort_keys=False, allow_unicode=True).strip() + "\n---\n\n" + body.strip() + "\n"

def write(name: str, description: str, body: str, type: str = "project",
          scope: str = "global", source: str = "agent", session: str = "") -> Memory:
    """Create or update one memory.

    Update rather than create when the name already exists, wherever it lives.
    That is the whole dedupe story and it is deliberate: an agent that writes
    the same fact twice under the same name has corrected itself, and an agent
    that wants a second fact has to name it differently, which is the moment it
    notices it already has one.
    """
    name = _safe_name(name)
    description = (description or "").strip()
    if not description: raise ValueError("description required -- it is the only part always in context")
    if len(description) > MAX_DESC: description = description[:MAX_DESC].rstrip() + "..."
    body = (body or "").strip()
    if not body: raise ValueError("body required")
    if len(body) > MAX_BODY:
        raise ValueError(f"body is {len(body)} chars, limit is {MAX_BODY}. Write the long version to a "
                         f"file and keep a `reference` memory pointing at it.")
    if type not in TYPES: raise ValueError(f"type must be one of {list(TYPES)}")
    if source not in SOURCES: raise ValueError(f"source must be one of {list(SOURCES)}")

    prior = all_memories().get(name)
    scope = _safe_scope(prior.scope if (prior and not scope) else scope)
    now = store.now_iso()
    meta = {"name": name, "description": description, "type": type, "scope": scope,
            "source": source, "created": (prior.created if prior else now), "updated": now,
            "session": session or (prior.session if prior else ""),
            "uses": (prior.uses if prior else 0)}
    if prior and prior.used: meta["used"] = prior.used
    p = _path_for(name, scope)
    p.write_text(_render(meta, body))
    # A memory that moved scope must not be left behind in the old one, or the
    # flat namespace has two files claiming one name and which wins is down to
    # sort order.
    if prior and prior.path != p:
        try: prior.path.unlink()
        except OSError: pass
    write_index()
    return _read(p)

def delete(name: str) -> dict:
    """Archived, never removed. A memory is deleted because it is wrong, and
    finding out later *how* it was wrong is worth a file nobody reads."""
    m = get(name)
    dest = archive_dir() / f"{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}-{m.name}.md"
    try: m.path.replace(dest)
    except OSError as e: raise ValueError(f"could not archive {name}: {e}")
    write_index()
    return {"deleted": m.name, "archived": str(dest)}

def touch(name: str):
    """Record that a memory was actually loaded.

    The counter is what makes pruning possible later without guessing: a
    memory nothing has read in months is a line of context nobody is paying
    for on purpose. Best effort -- failing to count a use must never fail the
    read that earned it.
    """
    try:
        m = get(name)
        meta, body = parse_frontmatter(m.path.read_text())
        meta["uses"] = int(meta.get("uses") or 0) + 1
        meta["used"] = store.now_iso()
        m.path.write_text(_render(meta, body))
    except Exception: pass

def search(query: str, limit: int = 8) -> list[dict]:
    """Substring hunt over descriptions and bodies, out of scope included.

    Deliberately dumber than the recall scorer: this one is called when the
    agent already knows what it is looking for, and a literal match on a path
    or an error string is exactly what it wants.
    """
    q = (query or "").strip().lower()
    if not q: return []
    terms = [t for t in re.split(r"\W+", q) if len(t) > 1]
    out = []
    for m in all_memories().values():
        hay = f"{m.name}\n{m.description}\n{m.body}".lower()
        hits = sum(hay.count(t) for t in terms)
        if q in hay: hits += 5
        if not hits: continue
        line = next((l.strip() for l in m.body.splitlines()
                     if any(t in l.lower() for t in terms)), m.description)
        out.append({"score": hits, **m.brief(), "line": line[:200]})
    return sorted(out, key=lambda d: -d["score"])[:limit]

def index_lines(path: str = None, apps: list = None) -> list[str]:
    return [f"- {m.name} [{m.scope}]: {m.description}" for m in in_scope(path, apps)]

def write_index():
    """MEMORY.md, regenerated from the files.

    Generated, never authored: the files are the truth and an index that can
    disagree with them is a second source that will. It exists for the human
    browsing ~/.cuacode by hand -- nothing here reads it back.
    """
    lines = ["# Memory", "",
             "Generated from the files in this directory. Edit the memories, not this.", ""]
    by_scope = {}
    for m in sorted(all_memories().values(), key=lambda m: (m.scope, m.name)):
        by_scope.setdefault(m.scope, []).append(m)
    for scope, items in by_scope.items():
        lines.append(f"## {scope}")
        lines += [f"- [{m.name}]({m.path.relative_to(root())}) — {m.description}" for m in items]
        lines.append("")
    (root() / "MEMORY.md").write_text("\n".join(lines))
