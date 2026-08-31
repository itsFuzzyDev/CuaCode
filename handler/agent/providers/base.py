"""Shared plumbing for the provider dialects: chunk normalization, usage
extraction, tool-call reassembly, message shapers, and the local-daemon probe.

Nothing here knows a provider's name; the dialect modules import from here."""

import json, socket
from dataclasses import dataclass, field
from urllib.parse import urlparse

def _segments(system) -> list[str]:
    """The system prompt as its parts.

    Callers hand over either one string or a list of them -- the instructions,
    then what is true about this machine, then whatever else. They are kept
    apart rather than concatenated because the three dialects disagree about
    what a system prompt even is: two of them take messages and can carry
    several, anthropic takes a request parameter that is a list of blocks. A
    caller that joined them first would have made that decision for all three,
    and made it wrong for the one that has a real place to put them.
    """
    if not system: return []
    if isinstance(system, str): system = [system]
    return [s for s in (str(x).strip() for x in system) if s]

def _int(obj, *names):
    """The first of these fields that is present and a number, attribute or
    key. Provider SDKs return objects, compatible servers return dicts, and
    both shapes turn up on the same wire format."""
    for n in names:
        v = obj.get(n) if isinstance(obj, dict) else getattr(obj, n, None)
        if isinstance(v, int): return v
    return None

def _sub(obj, *names):
    """A nested holder off a usage object, whichever of these names it uses."""
    for n in names:
        v = obj.get(n) if isinstance(obj, dict) else getattr(obj, n, None)
        if v is not None: return v
    return None

def usage_of(raw) -> dict:
    """Token counts off one streamed frame, as {"input": n, "output": n}.

    "reasoning" joins them when the provider breaks the reply down that far.
    Only openai's dialect does: anthropic bills thinking inside output_tokens
    and never says how much of it was thinking, so a caller that wants the
    number there has to estimate it from the text it already streamed.

    Only what the frame actually carried: a caller merges these, because no
    provider puts both halves in one place. Ollama counts on its final chunk,
    openai on a usage trailer, anthropic on message_start (prompt) and again on
    message_delta (reply). Nothing here asks for the numbers -- they are read
    off frames that are already being streamed for other reasons -- so a
    provider that never reports usage simply never contributes, and the readout
    that depends on it stays empty rather than showing a guess.

    Anthropic's input_tokens excludes cached prompt, which is most of the
    prompt on a run that is going well; the cache counters are added back, or
    a cached conversation would appear to shrink as it grows.
    """
    if raw is None: return {}
    out = {}

    # ollama: eval counters on the last chunk of the stream.
    if (n := _int(raw, "prompt_eval_count")) is not None: out["input"] = n
    if (n := _int(raw, "eval_count")) is not None: out["output"] = n

    # openai and anthropic both hang a usage object somewhere. message_start
    # keeps it one level down, on the message.
    for holder in (raw, getattr(raw, "message", None)):
        u = holder.get("usage") if isinstance(holder, dict) else getattr(holder, "usage", None)
        if u is None: continue
        if (n := _int(u, "prompt_tokens", "input_tokens")) is not None:
            out["input"] = n + (_int(u, "cache_read_input_tokens") or 0) \
                             + (_int(u, "cache_creation_input_tokens") or 0)
        if (n := _int(u, "completion_tokens", "output_tokens")) is not None:
            out["output"] = n
        det = _sub(u, "completion_tokens_details", "output_tokens_details")
        if det is not None and (n := _int(det, "reasoning_tokens")) is not None:
            out["reasoning"] = n
    return out

@dataclass
class Delta:
    """One normalized streaming chunk.

    tool_calls carries only *complete* calls. Ollama hands those over whole,
    but anthropic (input_json_delta) and openai (tool_calls[i].function
    .arguments) stream tool arguments as partial JSON spread across chunks.
    Those providers accumulate inside their own stream() and emit here once a
    call closes, so the loop never learns which kind it is talking to.

    raw keeps the untouched provider frame, so the last one can still be
    reported for usage and finish reason.
    """
    thinking: str = ""
    content: str = ""
    tool_calls: list = field(default_factory=list)
    raw: object = None

class CallAssembler:
    """Reassembles tool calls whose arguments arrive as JSON fragments.

    Ollama never needs this. It lives here because anthropic and openai both
    stream the same way and would otherwise each grow their own copy. close()
    returns a neutral {id, name, args}; the provider shapes it into whatever
    its own tool_calls look like.
    """
    def __init__(self): self._open = {}

    def start(self, key, name: str = "", call_id: str = ""):
        """Idempotent and merging: openai sends the id and name on the first
        fragment for an index, but not every compatible server does, so this
        can be called on each fragment with whatever that one happens to have."""
        c = self._open.setdefault(key, {"id": "", "name": "", "buf": ""})
        if name: c["name"] = name
        if call_id: c["id"] = call_id

    def feed(self, key, fragment: str):
        c = self._open.get(key)
        if c: c["buf"] += fragment

    def close(self, key) -> dict | None:
        """The finished call, or None if its arguments never parsed -- a
        truncated stream must not surface as a call with empty args."""
        c = self._open.pop(key, None)
        if not c: return None
        try: args = json.loads(c["buf"]) if c["buf"].strip() else {}
        except json.JSONDecodeError: return None
        return {"id": c["id"], "name": c["name"], "args": args}

    def close_all(self) -> list[dict]:
        return [c for c in (self.close(k) for k in list(self._open)) if c]

def _serialize(tc):
    for attr in ("model_dump", "dict"):
        fn = getattr(tc, attr, None)
        if fn: return fn()
    return tc

def attachment_note(images: list) -> str:
    """What the user called the files they attached.

    The model gets the pixels either way; this is the only way it gets the
    name, and a name is often the most useful sentence about a picture nobody
    wrote a sentence about -- "login-error.png", "Screenshot 2026-08-23 at
    14.02.11.png". Cheap enough to always send: a few tokens against an image
    that costs hundreds.

    One line rather than a tagged block, and the same line the mid-run path
    uses (see agent/main.py's _steer_note), so an attachment reads the same
    wherever in a turn it arrived.
    """
    names = [i.get("name") or "image" for i in (images or [])]
    return "[attached: " + ", ".join(names) + "]" if names else ""

def append_user_text(msg: dict, extra: str) -> None:
    """Fold a runtime block into a user turn, in place.

    Recall, the docs notice, a skill's instructions and the interrupt note all
    ride on the user's own message rather than following it, because two user
    messages in a row is a 400 on anthropic. That was a string concatenation
    until a user turn could carry images: now the content is a string in one
    dialect and a list of blocks in the other two, and the text has to find the
    text block rather than be appended to the list.

    A turn that is nothing but images grows its text block here. That is the
    only case where one is created rather than extended -- user_message leaves
    it out on purpose, an empty block being a 400 of its own -- and it goes in
    front of the images so the shape matches the one that had words in it.
    """
    if not extra: return
    content = msg.get("content")
    if content is None or isinstance(content, str):
        msg["content"] = content + "\n\n" + extra if content else extra
        return
    for block in content:
        if isinstance(block, dict) and block.get("type") == "text":
            block["text"] = block["text"] + "\n\n" + extra if block.get("text") else extra
            return
    content.insert(0, {"type": "text", "text": extra})

def _images(data) -> list:
    """Both shapes the tools produce: screenshot returns image_base64, photos
    returns an images list."""
    if not isinstance(data, dict): return []
    img, images = data.get("image_base64"), data.get("images") or []
    if img and not images: images = [img]
    return images

def _media_type(b64: str) -> str:
    """Read the format off the bytes rather than declaring one.

    It cannot be a constant: screenshot returns PNG and photos returns JPEG,
    and both were going out labelled jpeg. A label that disagrees with the
    bytes is rejected outright by some models and quietly mis-decoded by
    others -- and the tool it breaks is the one the agent sees the screen with.
    Base64 is deterministic at the front, so the magic bytes show through.
    """
    if b64.startswith("iVBORw0KGgo"): return "image/png"
    if b64.startswith("R0lGOD"): return "image/gif"
    if b64.startswith("UklGR"): return "image/webp"
    return "image/jpeg"

def _daemon_up(host: str | None, timeout: float = 0.5) -> bool:
    """Whether anything is listening at this address.

    For the local servers that speak a dialect over a base_url -- lm studio,
    llama.cpp, vllm -- where installed is not running and a configured entry
    with nothing behind it must not be offered as reachable. Any of `host`,
    `host:port` or a full url, so it is normalized to a (host, port) pair
    first, and an empty one means the usual ollama port. A TCP connect is all
    that is checked: the case
    this guards is nothing listening at all, and a real request right after
    reports anything subtler far better than a probe would.
    """
    host = (host or "").strip() or "127.0.0.1:11434"
    if "://" not in host: host = f"http://{host}"
    u = urlparse(host)
    try:
        with socket.create_connection((u.hostname or "127.0.0.1",
                                       u.port or (443 if u.scheme == "https" else 11434)), timeout):
            return True
    except OSError: return False