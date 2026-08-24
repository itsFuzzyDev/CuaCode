"""Instructions the agent did not write for itself.

Two kinds, and they are deliberately not the same mechanism.

The user's own AGENTS.md is theirs, applies everywhere, and is short by
construction, so it goes into the system prompt verbatim -- standing orders, in
front of the model on every turn without it having to go and look.

A project's documentation is none of those things. It belongs to the repository
rather than to the user, there can be five of it, and a README is routinely
longer than everything else in the window put together. So that one is a
pointer: the names and the sizes, once, and the agent spends a `file` read on
whichever of them turns out to matter. Same bargain as memory and skills, for
the same reason -- a wrong pointer costs a line, a wrong injected body costs the
context and then gets acted on.
"""
import os, re, sys

from handler.session import store

# The user's standing instructions. AGENTS.md is the name the wider tooling has
# settled on; the rest are accepted so nobody has to be told they used the wrong
# one. First hit wins, in this order.
USER_FILES = ("AGENTS.md", "agents.md", "AGENT.md", "CLAUDE.md")

# Enough for a page of house style and then some. Past this it is a document
# rather than an instruction, and a document does not belong in every request:
# the tail is dropped and said to be dropped, so a user wondering why the agent
# ignored the bottom of their file has an answer in front of the model.
MAX_USER = 8000

# What counts as documentation sitting in the working directory. A fixed list
# rather than every *.md: a repo with forty markdown files in its root would
# turn the pointer into the thing it is meant to avoid.
DOC_FILES = ("AGENTS.md", "AGENT.md", "CLAUDE.md", "GEMINI.md", "README.md",
             "CONTRIBUTING.md", "ARCHITECTURE.md", "DEVELOPMENT.md",
             ".cursorrules", ".windsurfrules")

# A doc bigger than this is a manual. It is still listed -- knowing it is there
# is the useful part -- and the size in the listing is what tells the agent to
# grep it rather than read it whole.
HUGE = 200_000

# How many turns must pass before the second pointer may fire. The first message
# and the one right after it are the same thought; a repeat there would read as
# nagging rather than as a reminder.
MIN_GAP = 2


# ---- the user's own instructions ----

_cache: dict = {}

def user_path():
    """The user's instruction file, or None."""
    home = store.home()
    for name in USER_FILES:
        p = home / name
        if p.is_file(): return p
    return None


def user_block() -> str:
    """~/.cuacode/AGENTS.md as a system segment, or "" when there is none.

    Cached on the file's identity and mtime, and that is not an optimisation:
    this string is part of the system prompt, providers cache the system prompt
    on its bytes, and a block rebuilt slightly differently each turn would throw
    that cache away every turn. Re-stat'd rather than read, so an edit still
    lands on the next turn without a restart.
    """
    p = user_path()
    if p is None: return ""
    try: st = p.stat()
    except OSError: return ""
    key = (str(p), st.st_mtime_ns, st.st_size)
    if _cache.get("key") == key: return _cache["text"]
    try: text = p.read_text().strip()
    except OSError: return ""
    if not text:
        block = ""
    else:
        cut = ""
        if len(text) > MAX_USER:
            text, cut = text[:MAX_USER].rstrip(), (
                f"\n\n[truncated at {MAX_USER} characters. The rest of the file was not sent -- "
                f"move the detail into a skill or a memory and keep this file to instructions.]")
        block = (f"<user_instructions source=\"{p}\">\n"
                 "Written by the user, for you, and in force in every conversation. Where it\n"
                 "disagrees with your default habits, it wins. Where it disagrees with what the\n"
                 "user is asking for right now, the request wins -- these are standing orders,\n"
                 "not a veto on the person giving them.\n\n"
                 f"{text}{cut}\n"
                 "</user_instructions>")
    _cache.update(key=key, text=block)
    return block


# ---- the project's documentation ----

def _read_files() -> set:
    """Absolute paths the file tool has read this session.

    Read off the same edit gate `file` maintains and the session restores on
    reload (handler/session/main.py), so a doc the agent has already opened --
    this turn or before the reload -- is never pointed at again.
    """
    m = sys.modules.get("_common")
    return {os.path.abspath(p) for p in getattr(m, "read_files", ())} if m else set()


def _size(n: int) -> str:
    return f"{n / 1000:.1f}k" if n >= 1000 else f"{n}b"


def docs_in(path: str) -> list:
    """Documentation in this directory, newest name first in DOC_FILES order.

    Non-recursive on purpose. The directory the conversation is happening in is
    the one whose conventions apply; walking the tree finds a dozen READMEs
    belonging to code nobody has mentioned.
    """
    if not path: return []
    try: entries = {e.name.lower(): e for e in os.scandir(path) if e.is_file()}
    except OSError: return []
    out = []
    for name in DOC_FILES:
        e = entries.get(name.lower())
        if not e: continue
        try: size = e.stat().st_size
        except OSError: continue
        if not size: continue
        out.append({"name": e.name, "path": os.path.abspath(e.path), "size": size})
    return out


# Per session: how many pointers have gone out, and at which turn the last one
# did. In memory for the same reason recall's is -- the judgement "already
# suggested, do not repeat" is about this conversation and means nothing after a
# restart.
_sent: dict[str, dict] = {}


def forget(sid: str):
    _sent.pop(sid or "-", None)


def _related(text: str, path: str, docs: list) -> bool:
    """Whether the message is about the project, rather than merely happening in
    its directory.

    The second pointer is spent here, so the bar is one solid word: the name of
    the directory, a word from inside it, or the name of a doc. A conversation
    that opened in a repo and then went off to talk about the weather does not
    get reminded about the README.
    """
    from integrations.memory import recall
    q = set(recall.tokens(text))
    if not q: return False
    known = set(recall.tokens(os.path.basename(path.rstrip("/\\"))))
    for d in docs: known |= set(recall.tokens(d["name"]))
    known |= {"repo", "repository", "project", "codebase", "readme", "docs", "convention", "conventions"}
    return bool(q & known)


def docs_block(text: str, sid: str = "", path: str = "", first: bool = False) -> str:
    """The project-docs pointer for one user message, or "".

    Fires twice at most in a conversation: once on the opening message, because
    that is when the agent is deciding how to approach the whole thing, and once
    more later only if the conversation turns out to be about this project and
    the docs are still unread. There is no third -- a pointer that keeps coming
    back is furniture, and then the one that mattered is furniture too.
    """
    state = _sent.setdefault(sid or "-", {"count": 0, "turn": -99})
    state["turn_no"] = turn = state.get("turn_no", -1) + 1
    if state["count"] >= 2: return ""

    docs = docs_in(path)
    if not docs: return ""
    read = _read_files()
    unread = [d for d in docs if d["path"] not in read]
    if not unread: return ""

    if state["count"] == 0:
        if not first: return ""
    else:
        if turn - state["turn"] < MIN_GAP: return ""
        if not _related(text, path, docs): return ""

    state["count"] += 1
    state["turn"] = turn
    lines = [f"- {d['name']} ({_size(d['size'])})" + (" - large, grep it rather than reading it whole"
                                                      if d["size"] > HUGE else "")
             for d in unread]
    again = "still unread" if state["count"] > 1 else "not read yet"
    return (f"project docs: {len(unread)} {again} in this directory\n\n"
            "<project_docs>\n"
            "Documentation you have not opened, in the directory this conversation is\n"
            f"happening in ({os.path.abspath(path)}). From the runtime, not the user, and\n"
            "nothing here has been read for you.\n"
            + "\n".join(lines) + "\n"
            "Read the ones that bear on what was asked with the file tool - instructions for\n"
            "agents (AGENTS.md, CLAUDE.md, .cursorrules) before anything else, since they may\n"
            "change how you are supposed to work here. Ignore the rest silently: do not list\n"
            "them back, do not explain that you skipped them.\n"
            "</project_docs>")
