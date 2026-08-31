"""Ollama: the hosted catalog, never a local daemon."""

import json, os

import ollama

from handler.agent.images import PinnedUser
from handler.agent.providers.base import Delta, _segments, _serialize, _images
from tools._parser.FromProvider import parse_tool_calls
from tools._parser.ToProvider import format_tool_result

class Ollama:
    """Ollama's hosted catalog, never a local daemon.

    A local daemon is a fine thing to have and the wrong thing to point this
    at. The agent opens every conversation with ~10k tokens of instructions
    and environment and carries twenty-odd tool schemas alongside it, which is
    a size the models anyone runs at home handle badly or not at all: the run
    does not fail, it just quietly stops following the prompt. So the host is
    ollama.com, always, and the picker lists what that account can actually
    reach rather than whatever happens to be on this disk.

    Both ways in work, and neither needs anything set here. An API key pasted
    into settings (or OLLAMA_API_KEY) is one; the desktop app's `ollama
    signin` is the other, which writes the same kind of key to ~/.ollama/keys
    and is read from there when no key was given.
    """
    name = "ollama"
    default_model = "minimax-m3"
    key_env = "OLLAMA_API_KEY"
    base_url = "https://ollama.com"
    vision = True

    def __init__(self): self._c, self._host = None, ""

    def setup(self, api_key: str | None):
        # The key is pasted, and ollama's docs show it inside an Authorization
        # header, so it arrives with the scheme attached about as often as
        # not. The client builds `Bearer {key}` itself, and `Bearer Bearer x`
        # is a 401 that reads as a bad key rather than a mangled one.
        key = (api_key or "").strip()
        if key.lower().startswith("bearer "): key = key[7:].strip()
        key = key or _signed_in_key()
        if key: os.environ['OLLAMA_API_KEY'] = key
        else: os.environ.pop('OLLAMA_API_KEY', None)
        # Set rather than read: OLLAMA_HOST on this machine points at the local
        # daemon whenever the app is installed, which is exactly the host this
        # provider is not for.
        os.environ['OLLAMA_HOST'] = self.base_url
        if self.base_url != self._host: self._host, self._c = self.base_url, None

    def _client(self):
        """Ours, not the module-level one.

        `ollama.chat` goes through a client built during `import ollama`,
        which resolves OLLAMA_HOST and OLLAMA_API_KEY once, in its
        constructor, and holds them. Anything setup() puts in the environment
        lands after that and is never read, so the key went unsent and the
        host stayed on the local daemon -- a connection refused on every turn
        with nothing listening there. Built here instead, after setup() has
        decided, and dropped whenever that decision changes.
        """
        if self._c is None: self._c = ollama.Client(host=self._host or self.base_url)
        return self._c

    def stored_key(self) -> str:
        """The signed-in account's key, for config to find without being told.
        See _signed_in_key."""
        return _signed_in_key()

    def stream(self, model: str, messages: list[dict], tools: list, system: str = "", params: dict = None):
        # The system prompt as leading messages, one per segment: most chat
        # templates render the list in a loop, and one that keeps only the first
        # would drop the later segments. If a model acts like it never read the
        # environment block, look here.
        msgs = [{"role": "system", "content": s} for s in _segments(system)] + messages
        # Ollama splits request options: a few sit at the top level, and the
        # rest -- num_ctx, temperature, top_p -- live under `options`.
        opts = dict(params or {})
        kw = {k: opts.pop(k) for k in ("keep_alive", "format", "think") if k in opts}
        kw.setdefault("think", True)
        stream = self._client().chat(model=model, messages=msgs, tools=tools, stream=True,
                                     options=opts or None, **kw)
        try:
            for chunk in stream:
                m = chunk.message
                yield Delta(thinking=getattr(m, 'thinking', None) or "",
                            content=getattr(m, 'content', None) or "",
                            tool_calls=[_serialize(tc) for tc in (getattr(m, 'tool_calls', None) or [])],
                            raw=chunk)
        finally:
            # Runs on normal exhaustion and on cancel, where the caller closes
            # this generator and GeneratorExit lands here.
            stream.close()

    def models(self) -> list | None:
        """The hosted catalog, or None if it will not say.

        This is ollama.com's /api/tags, not the local daemon's -- the same call
        `ollama list` makes, asked of the cloud. A local daemon answers the
        same endpoint with what is on this disk, plus `:cloud` aliases for
        anything pulled through it, and that mixed list is what the picker used
        to show. Both suffixed and bare names resolve here, so a config written
        against the old list keeps working.

        None and [] are different answers: an unreachable host has to leave the
        picker showing what is configured rather than claiming the account has
        no models.
        """
        try: return sorted(m.model for m in self._client().list().models)
        except Exception: return None

    def capabilities(self, model: str) -> list | None:
        """What this model can do, straight from the daemon, or None if it will
        not say.

        Ollama is the one provider here that answers the question directly:
        /api/show returns a capabilities list, and "vision" is in it or it is
        not. Everywhere else the only way to find out is to send an image and
        read the 400, which costs a turn. Where an authoritative answer exists,
        take it.
        """
        try: return list(getattr(self._client().show(model), "capabilities", None) or [])
        except Exception: return None

    def context_window(self, model: str) -> int:
        """The window this model runs with, from the daemon, or 0 if it will not
        say.

        Two different numbers live in /api/show and they are not interchangeable.
        modelinfo carries `<arch>.context_length`, which is what the weights were
        trained for; a num_ctx in the modelfile's parameters is what this daemon
        will actually allocate, and it is usually the smaller of the two. The
        effective one wins, because a gauge measuring against a window the
        process is not using is measuring against nothing.

        Ollama's own default when neither is set is smaller still, and is not
        reported here at all -- so this can read high on a stock model. Set
        num_ctx in the provider's params to pin both the request and the gauge.
        """
        try: show = self._client().show(model)
        except Exception: return 0
        for line in (getattr(show, "parameters", "") or "").splitlines():
            parts = line.split()
            if len(parts) == 2 and parts[0] == "num_ctx" and parts[1].isdigit():
                return int(parts[1])
        for key, value in dict(getattr(show, "modelinfo", None) or {}).items():
            if key.endswith("context_length") and isinstance(value, int):
                return value
        return 0

    def assistant_message(self, thinking: str, content: str, tool_calls: list) -> dict:
        return {"role": "assistant", "thinking": thinking, "content": content, "tool_calls": tool_calls}

    def user_message(self, text: str, images: list = None) -> dict:
        """A user turn that may carry images.

        result_messages already attaches images to *tool results*; this is the
        other direction -- opening a run with a picture, which is what a
        subagent asked to look at something needs. Same three dialects, same
        three shapes, so it lives next to its sibling rather than being
        rebuilt by every caller.
        """
        if not images: return {"role": "user", "content": text}
        return PinnedUser(role="user", content=text, images=list(images))

    def parse_calls(self, native: list) -> list:
        """Native tool_calls -> ToolCall list. Providers own this because the
        calls are not always a list: anthropic carries them as content blocks
        alongside text, with no tool_calls array to hand over at all."""
        return parse_tool_calls({"message": {"tool_calls": native}}, self.name)

    def result_messages(self, pairs: list) -> list[dict]:
        """One round's (call, result) pairs -> messages. Ollama takes one
        message per result. Anthropic and gemini require every result from a
        round batched into a single message, which is why the loop hands over
        the whole round at once instead of appending as each call returns."""
        out = []
        for call, result in pairs:
            images = _images((result or {}).get("result"))
            if images and not self.vision:
                # Replay guard, same as the other dialects: a sighted model's
                # history opened on a blind one would fail on this request and
                # every one after, the images never leaving the history.
                meta = {k: v for k, v in (result or {}).get("result", {}).items()
                        if k not in ("image_base64", "images")}
                out.append({"role": "tool", "content": json.dumps(
                    {"note": f"{len(images)} image(s) captured, not shown: this model cannot see images. "
                             "describe_image can have a sighted model answer one question about it",
                     **meta})})
                continue
            fmt = format_tool_result(call, result, self.name)
            out.extend(fmt) if isinstance(fmt, list) else out.append(fmt)
        return out

    def native_calls(self, calls: list[dict]) -> list[dict]:
        """Canonical calls -> native tool_calls. Only needed when a session is
        reopened under a provider other than the one that recorded it."""
        return [{"function": {"name": c["name"], "arguments": c["args"]}} for c in calls]

def _signed_in_key() -> str:
    """The key `ollama signin` left behind, or "".

    Signing in through the desktop app is the other half of "have an ollama
    account", and it never produces a key anyone pastes anywhere: it writes one
    to ~/.ollama/keys as `name\\tkey` lines. Same bearer token the web console
    hands out, so reading the first line is all that separates a signed-in
    machine from one that has to be configured by hand.

    OLLAMA_API_KEY is not consulted here -- the caller has already been given
    it, through config, before this is reached.
    """
    try:
        for line in open(os.path.expanduser("~/.ollama/keys")):
            parts = line.strip().split("\t")
            if len(parts) == 2 and parts[1]: return parts[1]
    except OSError: pass
    return ""