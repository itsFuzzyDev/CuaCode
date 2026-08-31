"""anthropic messages dialect: system as a request parameter, content blocks,
one user message per round's tool results."""

import json, os

from handler.agent.images import PinnedUser
from handler.agent.providers.base import Delta, CallAssembler, _segments, _images, _media_type
from tools._parser.FromProvider import ToolCall
from tools._parser.ToProvider import format_tool_result

class Anthropic:
    """The anthropic messages dialect.

    Three things separate it from the other two: the system prompt is a
    request parameter rather than a message, an assistant turn is a list of
    content blocks (thinking, text, tool_use) rather than parallel fields, and
    every tool result from one round has to arrive in a single user message.
    """
    name = "anthropic"
    default_model = "claude-opus-5"
    key_env = "ANTHROPIC_API_KEY"
    base_url = None
    vision = True

    def __init__(self):
        self._key, self._c, self._thinking = "", None, []

    def setup(self, api_key: str | None):
        key = api_key or os.environ.get(self.key_env) or ""
        if key != self._key: self._key, self._c = key, None

    def _client(self):
        if self._c is None:
            from anthropic import Anthropic as _SDK   # imported late, like openai's
            self._c = _SDK(api_key=self._key or "not-needed", base_url=self.base_url)
        return self._c

    def stream(self, model: str, messages: list[dict], tools: list, system: str = "", params: dict = None):
        kw = {"model": model, "messages": _cached(messages), "stream": True}
        # There is no system role in this dialect -- system is a request
        # parameter, and a {"role": "system"} message is rejected outright. It
        # is already a list of blocks, so the segments land here as themselves.
        # One breakpoint, on the last block: it covers everything ahead of it,
        # the earlier blocks and the tools, all byte-identical on every round.
        blocks = [{"type": "text", "text": s} for s in _segments(system)]
        if blocks:
            blocks[-1] = {**blocks[-1], "cache_control": {"type": "ephemeral"}}
            kw["system"] = blocks
        if tools: kw["tools"] = tools
        kw.update({k: v for k, v in (params or {}).items() if k not in kw})
        # max_tokens is required and covers thinking plus the reply, so it is
        # not a reply-length knob -- too low truncates mid-thought.
        kw.setdefault("max_tokens", 64000)
        # Adaptive is 4.6-and-later only; an older model wants
        # {"type": "enabled", "budget_tokens": N} passed through params.
        # display=summarized because the thinking text is streamed to the UI:
        # the default omits it and the frontend just sees a long pause.
        kw.setdefault("thinking", {"type": "adaptive", "display": "summarized"})
        stream = self._client().messages.create(**kw)
        asm, kinds, think, done = CallAssembler(), {}, {}, False
        try:
            for ev in stream:
                t = getattr(ev, "type", "")
                if t == "content_block_start":
                    b = ev.content_block
                    kinds[ev.index] = b.type
                    if b.type == "tool_use": asm.start(ev.index, b.name or "", b.id or "")
                    elif b.type == "thinking":
                        think[ev.index] = {"type": "thinking", "thinking": "", "signature": ""}
                    elif b.type == "redacted_thinking":
                        think[ev.index] = {"type": "redacted_thinking", "data": b.data}
                elif t == "content_block_delta":
                    d, dt = ev.delta, getattr(ev.delta, "type", "")
                    if dt == "text_delta": yield Delta(content=d.text, raw=ev)
                    elif dt == "thinking_delta":
                        think[ev.index]["thinking"] += d.thinking
                        yield Delta(thinking=d.thinking, raw=ev)
                    elif dt == "signature_delta": think[ev.index]["signature"] += d.signature
                    elif dt == "input_json_delta": asm.feed(ev.index, d.partial_json)
                elif t == "content_block_stop":
                    # Unlike openai, each block gets its own stop, so a call can
                    # be closed the moment its arguments finish.
                    if kinds.get(ev.index) == "tool_use":
                        c = asm.close(ev.index)
                        if c: yield Delta(tool_calls=[_anthropic_call(c)], raw=ev)
                elif t in ("message_start", "message_delta", "message_stop"):
                    # message_start carries the prompt's token count and nothing
                    # else anyone here wants; the other two carry the stop reason
                    # and the reply's.
                    yield Delta(raw=ev)
            done = True
        finally:
            # Held for assistant_message, which runs next in the same turn. A
            # cancel lands here with done False: the turn is rolled back, so
            # keeping its blocks would leak a stale signature into whatever
            # message is built next.
            self._thinking = [think[i] for i in sorted(think)] if done else []
            stream.close()

    def assistant_message(self, thinking: str, content: str, tool_calls: list) -> dict:
        """Blocks, in the order the model produced them.

        The thinking blocks are echoed back verbatim, signature included: with
        thinking on, a tool_use turn whose thinking was dropped or edited is
        rejected. The plain `thinking` string is what reaches the UI and disk,
        and carries no signature -- which is why a session replayed from disk
        rebuilds without them. That is fine: only the turn being continued is
        checked, and earlier turns' thinking is stripped server-side anyway.
        """
        blocks, self._thinking = list(self._thinking), []
        if content: blocks.append({"type": "text", "text": content})
        blocks.extend(tool_calls)
        # An empty content list is rejected outright.
        return {"role": "assistant", "content": blocks or
                [{"type": "text", "text": content or thinking or "(no output)"}]}

    def user_message(self, text: str, images: list = None) -> dict:
        if not images: return {"role": "user", "content": text}
        return PinnedUser(role="user", content=[
            *([{"type": "text", "text": text}] if text else []),
            *({"type": "image", "source": {"type": "base64", "media_type": _media_type(b),
                                           "data": b}} for b in images)])

    def models(self) -> list | None:
        try: return sorted(m.id for m in self._client().models.list())
        except Exception: return None

    def context_window(self, model: str) -> int:
        """0, always: this endpoint does not publish window sizes.

        /v1/models here returns an id, a display name and a date, and there is
        no other endpoint that answers the question. Written out rather than
        left to the fallback so that the absence is a stated fact about the API
        instead of something that looks like an oversight -- put the number in
        config.json's context_window and the gauge has its denominator.
        """
        return 0

    def parse_calls(self, native: list) -> list:
        out = []
        for i, b in enumerate(native):
            # native is the calls alone, as with every other provider -- the
            # text and thinking blocks are rebuilt by assistant_message. The
            # guard is for anything else that ends up in the list.
            if b.get("type", "tool_use") != "tool_use": continue
            out.append(ToolCall(b.get("id") or f"call_{i}", b.get("name", ""), b.get("input") or {}))
        return out

    def result_messages(self, pairs: list) -> list[dict]:
        """The whole round as one user message. Results split across several
        messages are rejected, and a tool_result has to answer a tool_use from
        the immediately preceding assistant turn."""
        blocks = []
        for call, result in pairs:
            data = (result or {}).get("result")
            images = _images(data)
            if not images:
                blocks.append(format_tool_result(call, result, self.name))
                continue
            meta = {k: v for k, v in data.items() if k not in ("image_base64", "images")}
            if not self.vision:
                # Replay only: a blind model is never offered an image-returning
                # tool, but a session recorded on a sighted one can land here.
                blocks.append({"type": "tool_result", "tool_use_id": call.id, "content": json.dumps(
                    {"note": f"{len(images)} image(s) captured, not shown: this model cannot see images. "
                             "describe_image can have a sighted model answer one question about it",
                     **meta})})
                continue
            # No companion user message needed, unlike openai: a tool_result
            # carries image blocks itself.
            blocks.append({"type": "tool_result", "tool_use_id": call.id, "content": [
                {"type": "text", "text": json.dumps(meta)},
                *({"type": "image", "source": {"type": "base64", "media_type": _media_type(b),
                                               "data": b}} for b in images)]})
        return [{"role": "user", "content": blocks}] if blocks else []

    def native_calls(self, calls: list[dict]) -> list[dict]:
        return [_anthropic_call({"id": c.get("id") or f"call_{i}", "name": c["name"], "args": c["args"]})
                for i, c in enumerate(calls)]

def _cached(msgs: list[dict]) -> list[dict]:
    """Mark the end of the history so the next round can read it back.

    An agent loop resends the whole transcript every round, screenshots and
    all, so a cache read pays for the write almost immediately. Copied rather
    than marked in place -- the caller goes on appending to this same list.
    """
    if not msgs: return msgs
    last = dict(msgs[-1])
    content = last.get("content")
    blocks = [{"type": "text", "text": content}] if isinstance(content, str) else list(content or [])
    if not blocks or not isinstance(blocks[-1], dict): return msgs
    blocks[-1] = {**blocks[-1], "cache_control": {"type": "ephemeral"}}
    last["content"] = blocks
    return msgs[:-1] + [last]

def _anthropic_call(c: dict) -> dict:
    return {"type": "tool_use", "id": c["id"], "name": c["name"], "input": c["args"]}