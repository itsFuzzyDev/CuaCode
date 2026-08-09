import hashlib
from pathlib import Path

IMAGE_KEYS = ("image_base64", "images")

# Images live outside messages.jsonl: a screenshot is ~0.5-2MB of base64 and
# stays in history forever, so inlining would mean re-reading tens of
# megabytes to open a session. The base64 *string* is stored verbatim, not
# the decoded bytes -- decode/re-encode is not byte-identical, and a restored
# session has to hand the model exactly what it saw the first time.

def put(blobs: Path, b64: str) -> dict:
    digest = hashlib.sha256(b64.encode()).hexdigest()
    blobs.mkdir(parents=True, exist_ok=True)
    f = blobs / f"{digest}.b64"
    # Content-addressed: an identical capture costs nothing the second time,
    # and an agent watching a static screen re-screenshots constantly.
    if not f.exists(): f.write_text(b64)
    return {"$blob": digest}

def get(blobs: Path, ref: dict) -> str:
    f = blobs / f"{ref['$blob']}.b64"
    if not f.exists(): raise FileNotFoundError(f"missing blob {ref['$blob']}")
    return f.read_text()

def split(value, blobs: Path):
    """Walk a dispatch result, swapping base64 payloads for refs. Recursive
    rather than keyed on the two known result shapes, so a tool that nests
    images somewhere new does not silently inline megabytes."""
    if isinstance(value, dict):
        out = {}
        for k, v in value.items():
            if k in IMAGE_KEYS and isinstance(v, str): out[k] = put(blobs, v)
            elif k in IMAGE_KEYS and isinstance(v, list):
                out[k] = [put(blobs, i) if isinstance(i, str) else split(i, blobs) for i in v]
            else: out[k] = split(v, blobs)
        return out
    if isinstance(value, list): return [split(v, blobs) for v in value]
    return value

def rehydrate(value, blobs: Path):
    """Inverse of split. Matches on the ref shape, so it needs no key list
    and stays correct if IMAGE_KEYS grows."""
    if isinstance(value, dict):
        if set(value) == {"$blob"}: return get(blobs, value)
        return {k: rehydrate(v, blobs) for k, v in value.items()}
    if isinstance(value, list): return [rehydrate(v, blobs) for v in value]
    return value
