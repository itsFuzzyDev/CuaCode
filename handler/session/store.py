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
def system_prompt(version: Literal["v1", "v2"] = "v1") -> str: return (_REPO / f"system_prompt.{version}.txt").read_text()

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
