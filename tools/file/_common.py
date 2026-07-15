from pathlib import Path

READ_CAP = 50_000   # chars, prevent huge files blowing up model context
MATCH_CAP = 100     # max glob/grep results
GREP_FILE_CAP = 1_000_000  # bytes, skip huge files when grepping

read_files: set[str] = set()  # paths read (or written) this session, gates edit

def resolve(args: dict) -> Path:
    raw = args.get("path")
    if not raw: raise ValueError("path required")
    return Path(raw).expanduser().resolve()
