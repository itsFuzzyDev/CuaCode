import json, os, shutil, uuid; from typing import Literal
from datetime import datetime, timezone
from pathlib import Path

HOME_ENV = "CUACODE_HOME"
_REPO = Path(__file__).resolve().parents[2]

def home() -> Path:
    """Root of everything persistent. CUACODE_HOME overrides it, for tests."""
    root = Path(os.environ.get(HOME_ENV) or "~/.cuacode").expanduser()
    root.mkdir(parents=True, exist_ok=True)
    return root

def sessions_root() -> Path:
    d = home() / "sessions"
    d.mkdir(parents=True, exist_ok=True)
    return d

# Read fresh on every load, never persisted into a session: editing the
# prompt has to reach old conversations too.
# v0 used to exist but i lowkey nuked it, you can rename v1 -> v0 and v2 -> v1 but I haven't been able to quit having v1 yet LOL
def system_prompt() -> str: return (_REPO / f"system_prompt.txt").read_text()

def tools_dir() -> Path: return _REPO / "tools"

def now_iso() -> str: return datetime.now(timezone.utc).isoformat(timespec="seconds")

def new_id() -> str:
    """Timestamp-prefixed, so a sorted listing is already chronological and
    list_sessions() never opens a meta.json just to order results."""
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    root = sessions_root()
    while True:
        sid = f"{stamp}-{uuid.uuid4().hex[:4]}"
        if not (root / sid).exists(): return sid

def safe_id(sid: str) -> str:
    """Ids arrive over IPC from the frontend. Reject anything that could
    resolve outside sessions_root() before it reaches a path join."""
    if not sid or not isinstance(sid, str): raise ValueError("session id required")
    if sid.startswith(".") or "/" in sid or "\\" in sid or "\x00" in sid:
        raise ValueError(f"bad session id: {sid!r}")
    return sid

def path(sid: str) -> Path: return sessions_root() / safe_id(sid)

def write_json(p: Path, data: dict):
    """Atomic: a crash mid-write leaves the previous file intact, not a
    half-written one that fails to parse on next boot."""
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=2))
    os.replace(tmp, p)

def read_json(p: Path) -> dict:
    try: return json.loads(p.read_text())
    except (OSError, json.JSONDecodeError): return {}

def read_jsonl(p: Path) -> list[dict]:
    if not p.exists(): return []
    out = []
    for line in p.read_text().splitlines():
        if not line.strip(): continue
        # A torn last line (killed mid-append) costs that one record, not
        # the whole conversation.
        try: out.append(json.loads(line))
        except json.JSONDecodeError: continue
    return out

def append_jsonl(p: Path, records: list[dict]):
    if not records: return
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "a") as f:
        for r in records: f.write(json.dumps(r) + "\n")
        f.flush()
        os.fsync(f.fileno())

def list_sessions() -> list[dict]:
    """Meta only, newest first. Never reads messages.jsonl -- a listing must
    not pay for megabytes of screenshot history."""
    out = []
    for d in sorted(sessions_root().iterdir(), reverse=True):
        if not d.is_dir(): continue
        meta = read_json(d / "meta.json")
        if meta: out.append(meta)
    return out

# ---- projects ----
# A project is a directory the user said matters, not any directory a session
# ever happened to start in. The list is the tree's whitelist; every session
# stays findable through the palette regardless.

def projects_path() -> Path: return home() / "projects.json"

def list_projects() -> list[str]:
    return [p for p in read_json(projects_path()).get("projects", []) if isinstance(p, str)]

def list_pinned() -> list[str]:
    """Project group names in pin order (oldest pin first). By name, not dir:
    the pin rides the tree's group, and the tree groups by basename."""
    return [p for p in read_json(projects_path()).get("pinned", []) if isinstance(p, str)]

def list_project_details() -> tuple[dict, dict]:
    """Custom display names and icons per promoted dir. The group key stays
    the basename; a custom name is render-only cosmetics."""
    data = read_json(projects_path())
    names = {d: n for d, n in (data.get("names") or {}).items() if isinstance(d, str) and isinstance(n, str)}
    icons = {d: i for d, i in (data.get("icons") or {}).items() if isinstance(d, str) and isinstance(i, str)}
    return names, icons

def rename_project(d: str, name: str, icon: str) -> tuple[dict, dict]:
    """Set a project's display name and/or icon. An icon is one of: one emoji
    (up to 8 chars), a gradient circle "grad:<n>", or an uploaded image
    "img:data:image/...;base64,..." as the page downscaled it (capped at
    96KB). Anything else is dropped rather than stored. Empty string clears
    the field, falling back to the plain folder. Any real directory may
    carry details - the launch repo's group is not promoted, and Boaz wants
    it editable like the rest; the fields only ever render on a group that
    exists in the tree."""
    p = _norm_dir(d)
    if not Path(p).is_dir(): raise ValueError(f"no such directory: {p}")
    data = read_json(projects_path())
    names, icons = data.get("names") or {}, data.get("icons") or {}
    name = (name or "").strip()[:60]
    icon = (icon or "").strip()
    ok = (icon.startswith("img:data:image/") and len(icon) <= 98304) \
        or (icon.startswith("grad:") and len(icon) <= 12) \
        or (not icon.startswith(("img:", "grad:")) and len(icon) <= 8)
    if not ok: icon = ""
    if name: names[p] = name
    else: names.pop(p, None)
    if icon: icons[p] = icon
    else: icons.pop(p, None)
    data["names"], data["icons"] = names, icons
    write_json(projects_path(), data)
    return names, icons

def pin_project(name: str, on: bool) -> list[str]:
    """Pin/unpin a project group. Idempotent; returns the pin order."""
    if not name or not isinstance(name, str): raise ValueError("project name required")
    cur = list_pinned()
    if on and name not in cur: cur.append(name)
    if not on and name in cur: cur.remove(name)
    data = read_json(projects_path())
    data["pinned"] = cur
    write_json(projects_path(), data)
    return cur

def move_project(name: str, dir_: str, delta: int) -> None:
    """Move a group one slot up/down: within the pin order for a pinned
    group, within the promotion order otherwise. The tree renders exactly
    these two orders, so this is all the reordering there is. No-op at the
    edges and for a group that is in neither list."""
    if not name or not isinstance(name, str): raise ValueError("project name required")
    delta = 1 if (delta or 0) > 0 else -1
    data = read_json(projects_path())
    pinned = [p for p in (data.get("pinned") or []) if isinstance(p, str)]
    if name in pinned:
        i = pinned.index(name)
        j = i + delta
        if 0 <= j < len(pinned):
            pinned[i], pinned[j] = pinned[j], pinned[i]
            data["pinned"] = pinned
            write_json(projects_path(), data)
        return
    dirs = [p for p in (data.get("projects") or []) if isinstance(p, str)]
    i = next((k for k, d in enumerate(dirs) if d == dir_), -1)
    if i < 0:  # by basename, since the tree only knows names
        i = next((k for k, d in enumerate(dirs) if Path(d).name == name), -1)
    j = i + delta
    if 0 <= i and 0 <= j < len(dirs):
        dirs[i], dirs[j] = dirs[j], dirs[i]
        data["projects"] = dirs
        write_json(projects_path(), data)

def _norm_dir(d: str) -> str:
    """Expand and normalize a directory argument. Not required to exist: the
    tree hands over basenames, and a promoted folder may since have moved."""
    if not d or not isinstance(d, str): raise ValueError("project dir required")
    return str(Path(d).expanduser().resolve())

def add_project(d: str) -> str:
    """Promote a directory. Idempotent; returns the normalized path."""
    p = Path(d).expanduser().resolve()
    if not p.is_dir(): raise ValueError(f"no such directory: {d}")
    p = str(p)
    cur = list_projects()
    if p not in cur:
        write_json(projects_path(), {"projects": cur + [p]})
    return p

def remove_project(d: str) -> str:
    """Demote by directory (or by basename, since the tree only knows names).
    Returns the removed path, empty when nothing went. The custom name and
    icon go with it - details of a demoted project are not kept."""
    cur = list_projects()
    want = _norm_dir(d).lower()
    gone = next((p for p in cur if p.lower() == want or Path(p).name.lower() == Path(want).name), "")
    if gone:
        data = read_json(projects_path())
        data["projects"] = [p for p in cur if p != gone]
        (data.get("names") or {}).pop(gone, None)
        (data.get("icons") or {}).pop(gone, None)
        write_json(projects_path(), data)
    return gone

def transcript(sid: str, turns: int = 12, cap: int = 6000) -> dict:
    """What was said in a past conversation, in text.

    The counterpart to list_sessions() refusing to open messages.jsonl: a
    listing must stay cheap, but once something has decided *this* session is
    the one, there has to be a way to actually read it. Reading one on purpose
    is the only time that cost is worth paying.

    Text only, and only the tail. Screenshots, thinking, and tool results are
    the bulk of a transcript and almost none of its meaning -- what a later
    conversation needs is what was asked and what was concluded, so a tool call
    is kept as its name and nothing else.
    """
    meta = read_json(path(sid) / "meta.json")
    if not meta: raise ValueError(f"no session {sid!r}")
    lines, tools = [], []
    for r in read_jsonl(path(sid) / "messages.jsonl"):
        t = r.get("t")
        if t == "user":
            if tools: lines.append(f"[tools: {', '.join(tools)}]"); tools = []
            if (x := (r.get("text") or "").strip()): lines.append(f"user: {x}")
        elif t == "assistant":
            # Names, not results. A tool result is the largest thing in the file
            # and the least re-readable: what matters later is that the shell was
            # run, not the eighty lines it printed.
            tools += [c.get("name", "?") for c in (r.get("calls") or [])]
            if (x := (r.get("content") or "").strip()):
                if tools: lines.append(f"[tools: {', '.join(tools)}]"); tools = []
                lines.append(f"assistant: {x}")
    if tools: lines.append(f"[tools: {', '.join(tools)}]")
    # The opening survives the trim, always. The tail says how it ended and the
    # first line says what it was ever about -- keeping only the tail of a long
    # session hands back a conclusion with nothing to attach it to.
    keep = lines[-max(turns, 1) * 2:]
    opening = next((l for l in lines if l.startswith("user: ")), "")
    if opening and opening not in keep: keep = [opening, "..."] + keep
    text = "\n\n".join(keep)
    clipped = len(text) > cap or len(keep) < len(lines)
    # Trimmed from the front, because the tail is the conclusion and the
    # conclusion is the reason anyone reopened this.
    if len(text) > cap: text = "..." + text[-cap:]
    return {"id": meta.get("id", sid), "title": meta.get("title", ""), "cwd": meta.get("cwd", ""),
            "updated": meta.get("updated", ""), "turns": meta.get("turns", 0),
            "transcript": text, "clipped": clipped}

def delete(sid: str) -> bool:
    d = path(sid)
    if not d.is_dir(): return False
    shutil.rmtree(d)
    return True


def archive(sid: str) -> bool:
    """Move a session out of the store into the archive: gone from every
    listing, back by moving the folder in. The trash can, not the dump."""
    d = path(sid)
    if not d.is_dir(): return False
    out = home() / "archive" / "sessions"
    out.mkdir(parents=True, exist_ok=True)
    try:
        os.rename(d, out / safe_id(sid))
    except OSError:
        return False   # a folder of that id is already in the archive
    return True

def pin_session(sid: str, on: bool) -> str:
    """Pin a conversation for the tree: a ts on the meta, order being oldest
    pin first. Returns the ts, empty when unpinned. Not for the worker's own
    open session - its commit() rewrites meta.json from memory, so a pin set
    behind its back would be lost; callers pin that one through the Session."""
    d = path(sid)
    meta = read_json(d / "meta.json")
    if not meta: raise ValueError(f"no session {sid!r}")
    if on: meta["pinned"] = now_iso()
    else: meta.pop("pinned", None)
    write_json(d / "meta.json", meta)
    return meta.get("pinned", "")
