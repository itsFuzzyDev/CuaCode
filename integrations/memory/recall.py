"""What the user just said, matched against what is already known.

Pointers, never bodies. A hit puts one line in front of the model -- a name and
the sentence that says what it is -- and the model decides whether to spend a
tool call opening it. That asymmetry is the whole design: a wrong pointer costs
a line, a wrong injected body costs the context and, worse, gets acted on.

Runs on every user message, so it is lexical and structural only. No model
call, no network, no embedding: this sits between the user pressing enter and
the request going out, and anything that takes a second there is felt on every
single turn. The signals that pay for themselves are the cheap ones anyway --
which directory this is, which files were touched, which words came back.
"""
import os, re, time
from datetime import datetime, timezone

from handler.session import store
from integrations.memory import loader

MAX_MEMORIES = 3
MAX_SESSIONS = 2
# Two bars, because the two corpora are not alike. A memory's description is a
# sentence someone wrote to be recognised by, and there are tens of them, so one
# solid word is real evidence. Session titles are generated, there are hundreds,
# and one shared word between two of them means nothing at all.
MIN_MEMORY = 1.0
MIN_SESSION = 1.6
SESSION_POOL = 60         # newest sessions considered; older than that, search finds them by hand
HALF_LIFE_DAYS = 10.0

STOP = {
    "the","a","an","and","or","but","if","then","that","this","these","those","is","are","was",
    "were","be","been","being","to","of","in","on","at","for","with","from","by","as","it","its",
    "i","you","we","they","he","she","me","my","your","our","their","do","does","did","doing",
    "can","could","should","would","will","shall","may","might","must","have","has","had","not",
    "no","yes","so","just","also","get","got","make","made","let","lets","please","thanks","hi",
    "hey","hello","yo","ok","okay","what","when","where","which","who","why","how","again","now",
    "up","out","about","into","over","after","before","more","most","some","any","all","one","two",
}

# Words that point at something the sentence never named. When one of these
# turns up, the thing being pointed at is almost always the last thing that
# happened here rather than whatever shares a keyword with it -- so recency
# stops being a tiebreak and starts being the signal.
DEIXIS = re.compile(r"\b(that|those|it|again|earlier|before|last time|yesterday|the other day|"
                    r"we were|we did|you did|like we|same as|continue|resume|pick up|back to)\b", re.I)

def tokens(text: str) -> list[str]:
    """Words, plus the pieces of any path or identifier in them.

    `handler/session/store.py` has to match a session that touched
    `store.py`, and splitting on non-alphanumerics is what makes that happen
    without a special case for paths.
    """
    out = []
    for raw in re.split(r"[^A-Za-z0-9_./\\-]+", (text or "").lower()):
        if not raw: continue
        for piece in re.split(r"[^a-z0-9]+", raw):
            if len(piece) > 1 and piece not in STOP and not piece.isdigit(): out.append(piece)
    return out

def _weights(docs: list[list[str]]) -> dict:
    """Rarity weights. A token in every document says nothing about which one
    is meant; a token in exactly one says everything."""
    n = max(len(docs), 1)
    df = {}
    for d in docs:
        for t in set(d): df[t] = df.get(t, 0) + 1
    return {t: (n / c) ** 0.5 for t, c in df.items()}

def _score(q: set, doc: list[str], w: dict) -> float:
    seen = set(doc)
    return sum(w.get(t, 1.0) for t in q if t in seen)

def _age_days(iso: str) -> float:
    try: return max((datetime.now(timezone.utc) - datetime.fromisoformat(iso)).total_seconds(), 0) / 86400
    except Exception: return 999.0

def _ago(iso: str) -> str:
    d = _age_days(iso)
    if d < 1 / 24: return "just now"
    if d < 1: return f"{int(d * 24)}h ago"
    if d < 30: return f"{int(d)}d ago"
    return f"{int(d / 30)}mo ago"

# Suggested-already, per session. In memory on purpose: the point is not to
# repeat a pointer the model has already declined once, and that judgement does
# not survive a restart in any useful form.
_shown: dict[str, set] = {}

def _fresh(sid: str, key: str) -> bool:
    seen = _shown.setdefault(sid or "-", set())
    if key in seen: return False
    seen.add(key)
    return True

def forget(sid: str):
    _shown.pop(sid or "-", None)

def _memory_hits(q: set, path: str, apps: list) -> list:
    mems = loader.in_scope(path, apps)
    if not mems: return []
    docs = [tokens(f"{m.name} {m.description} {m.type}") for m in mems]
    w = _weights(docs)
    out = []
    for m, d in zip(mems, docs):
        # The name is the fact's identity -- someone chose those words to be the
        # handle for exactly this -- so matching one counts for more than
        # matching the same word somewhere in the sentence.
        s = _score(q, d, w) + 0.5 * len(q & set(tokens(m.name)))
        if s >= MIN_MEMORY: out.append((s, m))
    return sorted(out, key=lambda p: -p[0])

def _session_hits(q: set, path: str, sid: str, deictic: bool) -> list:
    """Past conversations worth reopening.

    Reads meta only. list_sessions() refuses to open messages.jsonl on purpose
    and this must not be the thing that makes it -- scoring a hundred sessions
    would otherwise mean reading a hundred conversations' worth of screenshots.
    """
    here = os.path.abspath(os.path.expanduser(path)) if path else ""
    metas = [m for m in store.list_sessions()[:SESSION_POOL]
             if m.get("id") != sid and (m.get("title") or m.get("cwd"))]
    if not metas: return []
    docs = [tokens(" ".join([m.get("title", ""), os.path.basename(m.get("cwd", "") or ""),
                             " ".join(os.path.basename(f.get("path", "")) for f in (m.get("read_files") or [])[:20])]))
            for m in metas]
    w = _weights(docs)
    out = []
    for m, d in zip(metas, docs):
        same_dir = bool(here and m.get("cwd") and os.path.abspath(m["cwd"]) == here)
        s = _score(q, d, w)
        # Same directory is the strongest signal available and it costs nothing
        # to compute, so it stands on its own: "what did we do here" needs to
        # find this session even when the words share nothing.
        if same_dir: s += 1.4
        if not s: continue
        # Recency decays a match rather than making one. A deictic prompt names
        # nothing, so there it is allowed to carry the whole score.
        recency = 0.5 ** (_age_days(m.get("updated", "")) / HALF_LIFE_DAYS)
        s = s * (0.6 + 0.4 * recency) + (1.5 * recency if deictic and same_dir else 0)
        if s >= MIN_SESSION: out.append((s, m))
    return sorted(out, key=lambda p: -p[0])

def block(text: str, sid: str = "", path: str = None, apps: list = None) -> str:
    """The recall note for one user message, or "" when nothing matched.

    Empty is the expected answer most turns. A block that appears every time is
    a block that gets read as furniture, and then the one that mattered is
    furniture too.
    """
    q = set(tokens(text))
    if not q and not DEIXIS.search(text or ""): return ""
    path = path if path is not None else loader.cwd()
    deictic = bool(DEIXIS.search(text or ""))

    lines = []
    try:
        for s, m in _memory_hits(q, path, apps):
            if len(lines) >= MAX_MEMORIES: break
            if _fresh(sid, f"m:{m.name}"): lines.append(f"- memory `{m.name}` - {m.description}")
    except Exception: pass
    n = 0
    try:
        for s, meta in _session_hits(q, path, sid, deictic):
            if n >= MAX_SESSIONS: break
            if not _fresh(sid, f"s:{meta['id']}"): continue
            title = (meta.get("title") or "").strip() or "untitled"
            where = "same directory" if (path and meta.get("cwd") and
                                         os.path.abspath(meta["cwd"]) == os.path.abspath(path)) else ""
            tail = ", ".join(x for x in (f"{meta.get('turns', 0)} turns", _ago(meta.get("updated", "")), where) if x)
            lines.append(f"- session `{meta['id']}` \"{title}\" - {tail}")
            n += 1
    except Exception: pass

    if not lines: return ""
    # The first paragraph is a sentence for the human -- frontends draw a
    # notice's opening paragraph and drop the rest, because the rest is
    # addressed to the model and reads on screen like the agent muttering.
    return (f"recall: {len(lines)} possibly related item{'s' if len(lines) > 1 else ''}, not loaded\n\n"
            "<recall>\n"
            "Matched against your names and titles by word overlap alone. Nothing here has been\n"
            "read, and a match is not evidence of relevance. Open one with the memory tool if it\n"
            "bears on what was asked; ignore the rest silently -- do not mention them.\n"
            + "\n".join(lines) + "\n</recall>")
