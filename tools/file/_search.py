import json, re, shutil, subprocess
from pathlib import Path

from _common import IGNORE_DIRS, MATCH_CAP, FILE_MATCH_CAP, GREP_FILE_CAP

RG_TIMEOUT = 60
MAX_CONTEXT = 10

def glob(path: Path, args: dict, ctx=None) -> dict:
    pattern = args.get("pattern")
    if not pattern: return {"error": "pattern required"}
    if not path.is_dir(): return {"error": f"not a directory: {path}"}
    keep = [m for m in path.glob(pattern) if not _ignored(m, path, args)]
    # Newest first. When a pattern matches more than the cap, the files somebody
    # touched today are the ones the question is about; alphabetical order answers
    # with whatever happens to start with 'a'.
    keep.sort(key=_mtime, reverse=True)
    return {"path": str(path), "matches": [str(m) for m in keep[:MATCH_CAP]],
            "count": len(keep), "truncated": len(keep) > MATCH_CAP}

def grep(path: Path, args: dict, ctx=None) -> dict:
    pattern = args.get("pattern")
    if not pattern: return {"error": "pattern required"}
    if not path.exists(): return {"error": f"not found: {path}"}
    if rg := shutil.which("rg"):
        # Two orders of magnitude faster on a real tree, and it honours .gitignore,
        # which no amount of hardcoded directory names ever quite does. None back
        # means rg would not answer -- a pattern in a dialect it does not speak,
        # or no rg at all -- and the walk answers instead.
        if (out := _rg(rg, path, args, pattern)) is not None: return out
    return _walk(path, args, pattern)

def _rg(rg: str, path: Path, args: dict, pattern: str):
    cmd = [rg, "--regexp", pattern, "--max-filesize", str(GREP_FILE_CAP)]
    if args.get("ignore_case"): cmd.append("--ignore-case")
    if args.get("include"): cmd += ["--glob", args["include"]]
    if args.get("no_ignore"): cmd += ["--no-ignore", "--hidden"]
    # rg prunes node_modules and friends by reading .gitignore, which a tree that
    # has no .gitignore -- or is not a repo at all -- does not get. Spelled out
    # here so both paths through this tool skip the same directories. After the
    # include glob, because rg lets the later glob win.
    elif not (set(path.parts) & IGNORE_DIRS):
        cmd += [g for d in sorted(IGNORE_DIRS) for g in ("--glob", f"!**/{d}/**")]
    if args.get("files_only"): cmd.append("--files-with-matches")
    else:
        cmd.append("--json")
        if n := args.get("context"): cmd += ["--context", str(min(n, MAX_CONTEXT))]
    try:
        p = subprocess.run(cmd + ["--", str(path)], capture_output=True, text=True,
                           timeout=RG_TIMEOUT)
    except (OSError, subprocess.SubprocessError):
        return None
    if p.returncode > 1: return None          # 1 is "no matches"; 2 and up is rg refusing
    if args.get("files_only"): return _files(p.stdout.splitlines(), path)

    matches, per_file, truncated = [], {}, False
    each = FILE_MATCH_CAP if path.is_dir() else MATCH_CAP
    for line in p.stdout.splitlines():
        try: ev = json.loads(line)
        except ValueError: continue
        kind = ev.get("type")
        if kind not in ("match", "context"): continue
        d = ev.get("data") or {}
        f, n = (d.get("path") or {}).get("text"), d.get("line_number")
        if f is None or n is None: continue    # a path or offset rg could only give as bytes
        if kind == "match":
            if per_file.get(f, 0) >= each: truncated = True; continue
            per_file[f] = per_file.get(f, 0) + 1
        if len(matches) >= MATCH_CAP: truncated = True; break
        hit = {"file": f, "line": n, "text": (d.get("lines") or {}).get("text", "").strip()[:500]}
        if kind == "context": hit["context"] = True
        matches.append(hit)
    return {"path": str(path), "matches": matches, "files": len(per_file),
            "truncated": truncated}

def _walk(path: Path, args: dict, pattern: str) -> dict:
    try: rx = re.compile(pattern, re.IGNORECASE if args.get("ignore_case") else 0)
    except re.error as e: return {"error": f"bad regex: {e}"}
    files = [path] if path.is_file() else [
        f for f in sorted(path.rglob(args.get("include") or "*"))
        if f.is_file() and not _ignored(f, path, args) and _small(f) and _is_text(f)]

    around = min(args.get("context") or 0, MAX_CONTEXT)
    each = FILE_MATCH_CAP if path.is_dir() else MATCH_CAP
    only = bool(args.get("files_only"))
    matches, hit_files, truncated = [], [], False
    for idx, f in enumerate(files):
        try: lines = f.read_text(errors="replace").splitlines()
        except OSError: continue
        hits = [i for i, l in enumerate(lines) if rx.search(l)]
        if not hits: continue
        hit_files.append(str(f))
        if only:
            if len(hit_files) >= MATCH_CAP:
                truncated = truncated or idx < len(files) - 1
                break
            continue
        if len(hits) > each: hits, truncated = hits[:each], True
        seen = set(hits)
        # The windows around two nearby matches overlap; unioning the line numbers
        # first is what keeps a line from being reported once as a hit and again as
        # somebody else's context.
        for j in sorted({j for i in hits for j in range(max(i - around, 0),
                                                        min(i + around + 1, len(lines)))}):
            if len(matches) >= MATCH_CAP: truncated = True; break
            hit = {"file": str(f), "line": j + 1, "text": lines[j].strip()[:500]}
            if j not in seen: hit["context"] = True
            matches.append(hit)
        if len(matches) >= MATCH_CAP:
            truncated = truncated or idx < len(files) - 1
            break
    if only:
        return {"path": str(path), "files": hit_files[:MATCH_CAP],
                "count": len(hit_files), "truncated": truncated}
    return {"path": str(path), "matches": matches, "files": len(hit_files),
            "truncated": truncated}

def _files(paths: list, base: Path) -> dict:
    return {"path": str(base), "files": paths[:MATCH_CAP], "count": len(paths),
            "truncated": len(paths) > MATCH_CAP}

def _ignored(p: Path, base: Path, args: dict) -> bool:
    """Whether a match lives somewhere nobody meant to search.

    Only the directories below `base` are checked, never `base` itself: a user who
    points the tool straight at node_modules is asking about node_modules.
    """
    if args.get("no_ignore"): return False
    try: parts = p.relative_to(base).parts[:-1]
    except ValueError: return False
    return any(part in IGNORE_DIRS for part in parts)

def _mtime(p: Path) -> float:
    try: return p.stat().st_mtime
    except OSError: return 0.0

def _small(p: Path) -> bool:
    try: return p.stat().st_size <= GREP_FILE_CAP
    except OSError: return False

def _is_text(p: Path) -> bool:
    try:
        with p.open("rb") as f: return b"\x00" not in f.read(1024)
    except OSError: return False
