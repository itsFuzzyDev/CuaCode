import os, tempfile
from pathlib import Path

READ_CAP = 50_000          # chars per read, keeps a huge file out of the model's context
READ_LINES = 2_000         # lines per read; the cap that usually bites first
MATCH_CAP = 100            # max glob/grep results
FILE_MATCH_CAP = 20        # max grep matches from any one file, when a directory was
                           # asked for -- one generated file should not spend the budget
GREP_FILE_CAP = 1_000_000  # bytes, skip huge files when grepping
DIFF_CAP = 200             # lines of diff echoed back after a write

# Pruned from every walk. Not a gitignore implementation: ripgrep brings the real
# thing when it is installed, and this is what is left when it is not.
IGNORE_DIRS = {".git", ".hg", ".svn", "node_modules", "__pycache__", ".venv",
               "venv", ".mypy_cache", ".pytest_cache", ".ruff_cache", ".tox",
               "dist", "build", ".next", "target", ".idea", ".gradle"}

# Paths read (or written) this session, gates edit. A plain set of strings because
# handler/session persists it between runs and re-seeds it by name.
read_files: set[str] = set()

# What those files looked like when they were read: enough to notice one moved
# underneath us, plus how to write it back in the shape it was found. Absent for a
# path restored from a saved session -- that one was checked against disk at restore
# time instead, which is the same answer arrived at earlier.
seen: dict[str, dict] = {}

def _base(ctx) -> str:
    d = (ctx or {}).get("cwd") if hasattr(ctx, "get") else None
    return d if d and Path(d).is_dir() else str(Path.home())

def resolve(args: dict, ctx=None) -> Path:
    """A relative path the way the user meant it.

    Joined to the directory the session was launched from, not the worker's own
    cwd: launched from an app bundle that is / or the bundle itself, and a model
    writing "notes.md" would land somewhere nobody would think to look for it.
    """
    raw = args.get("path")
    if not raw: raise ValueError("path required")
    p = Path(raw).expanduser()
    if not p.is_absolute(): p = Path(_base(ctx)) / p
    return p.resolve()

def load(path: Path) -> dict:
    """The file as text, plus what it takes to put it back unchanged.

    Newline style, encoding and the trailing newline are recorded rather than
    normalised away, because a tool that rewrites CRLF as LF turns a three-line
    edit into a diff against every line in the file.
    """
    raw = path.read_bytes()
    st = path.stat()
    enc = "utf-8"
    if raw.startswith(b"\xef\xbb\xbf"): enc = "utf-8-sig"
    elif raw.startswith((b"\xff\xfe", b"\xfe\xff")): enc = "utf-16"
    meta = {"mtime_ns": st.st_mtime_ns, "size": st.st_size, "mode": st.st_mode,
            "encoding": enc, "newline": "\r\n" if b"\r\n" in raw else "\n",
            "final_nl": raw.endswith((b"\n", b"\r")), "lossy": False}
    try:
        text = raw.decode(enc)
    except (UnicodeDecodeError, UnicodeError):
        # Readable, but not writable: re-encoding the replacement characters would
        # silently destroy the bytes they stand in for, so edit and write refuse.
        text, meta["lossy"] = raw.decode("utf-8", errors="replace"), True
    # The raw bytes ride along: a snapshot has to restore what was there,
    # not a re-encoding of what it decoded to.
    return {"text": text.replace("\r\n", "\n"), "raw": raw, "meta": meta}

def save(path: Path, text: str, meta: dict | None = None) -> dict:
    """Written whole, atomically, in the shape the file already had.

    A crash partway through write_text leaves a truncated source file behind. A
    rename cannot be seen half done, so the worst case here is the old file.
    Returns the fingerprint of what is now on disk.
    """
    meta = meta or {}
    text = text.replace("\r\n", "\n")
    if meta.get("final_nl") and text and not text.endswith("\n"): text += "\n"
    data = text.replace("\n", meta.get("newline", "\n")).encode(meta.get("encoding", "utf-8"))
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as f: f.write(data)
        # mkstemp is 0600 by design, and inheriting that would quietly strip the
        # execute bit off a script the agent only meant to edit a line of.
        os.chmod(tmp, (meta.get("mode") or 0o644) & 0o7777)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    st = path.stat()
    return {**meta, "mtime_ns": st.st_mtime_ns, "size": st.st_size, "mode": st.st_mode}

def record(path: Path, meta: dict) -> None:
    """Mark a file read: the gate edit checks, and what it checks against."""
    read_files.add(str(path))
    seen[str(path)] = meta

def stale(path: Path) -> str | None:
    """Whether the file moved since the agent last saw it.

    The gate on its own only proves a path was read at some point, which is the
    wrong question after a build script, a formatter or the user has rewritten the
    file in between -- an edit against remembered content silently drops their work.
    A missing fingerprint is not staleness; see `seen`.
    """
    prev = seen.get(str(path))
    if not prev: return None
    try: st = path.stat()
    except OSError as e: return f"cannot stat {path}: {e}"
    if st.st_mtime_ns == prev["mtime_ns"] and st.st_size == prev["size"]: return None
    return f"{path} changed on disk since you read it -- read it again first"
