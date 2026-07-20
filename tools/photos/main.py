import base64, io, os, urllib.request
from pathlib import Path
from PIL import Image

def _fetch(source: str) -> bytes:
    if source.startswith(("http://", "https://")):
        req = urllib.request.Request(source, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.read()
    path = Path(os.path.expanduser(source)).expanduser().resolve()
    if not path.exists():
        raise FileNotFoundError(f"Photo not found: {source}")
    return path.read_bytes()

def _to_b64(data: bytes, max_size: int | None = None) -> str:
    img = Image.open(io.BytesIO(data))
    # Convert to RGB if necessary so JPEG save always works
    if img.mode in ("RGBA", "P", "LA", "L"):
        img = img.convert("RGB")
    if max_size is not None:
        w, h = img.size
        if w > max_size or h > max_size:
            img.thumbnail((max_size, max_size), Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=86, optimize=True)
    return base64.b64encode(buf.getvalue()).decode()

def run(args: dict, ctx) -> dict:
    sources = args.get("sources", [])
    if not sources:
        raise ValueError("No photo sources provided. Pass URLs or file paths in `sources`.")
    # Default cap to 1920px so phone-camera photos don't blow the context window
    max_size = args.get("max_size", 1920)
    results = {"images": [], "errors": [], "count": 0}
    for src in sources:
        try:
            data = _fetch(src)
            b64 = _to_b64(data, max_size)
            results["images"].append(b64)
            results["count"] += 1
        except Exception as e:
            results["errors"].append({"source": src, "error": str(e)})
    if not results["images"]:
        raise RuntimeError(f"No photos loaded. Errors: {results['errors']}")
    return results