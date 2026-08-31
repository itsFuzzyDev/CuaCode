"""openai chat-completions dialect: everything speaking that wire format."""

import json, os

from handler.agent.images import PinnedUser
from handler.agent.providers.base import Delta, CallAssembler, _segments, _images, _media_type
from tools._parser.FromProvider import ToolCall

class OpenAI:
    """The openai chat-completions dialect.

    `name` is the schema dialect, not the registry key: everything that speaks
    this wire format (openrouter, groq, deepseek, together, vllm, lm studio)
    is the same class with a different base_url, registered under its own key.
    """
    name = "openai"

    def __init__(self, default_model="gpt-5.6-sol", base_url=None, key_env="OPENAI_API_KEY", vision=True):
        self.default_model, self.base_url, self.key_env = default_model, base_url, key_env
        self.vision = vision
        self._key, self._c = "", None

    def setup(self, api_key: str | None):
        key = api_key or os.environ.get(self.key_env) or ""
        if key != self._key: self._key, self._c = key, None

    def _client(self):
        if self._c is None:
            from openai import OpenAI as _SDK      # imported late: ollama-only runs never pay for it
            # Local servers (lm studio, llama.cpp, vllm) want no key but the
            # SDK refuses to build without one.
            self._c = _SDK(api_key=self._key or "not-needed", base_url=self.base_url)
        return self._c

    def stream(self, model: str, messages: list[dict], tools: list, system: str = "", params: dict = None):
        # One system message per segment. This dialect accepts several and
        # keeps them in order, so nothing has to be joined.
        msgs = [{"role": "system", "content": s} for s in _segments(system)] + messages
        kw = {"model": model, "messages": msgs, "stream": True}
        if tools: kw["tools"] = tools
        # Verbatim passthrough -- max_tokens, temperature, reasoning_effort,
        # top_p. The keys are the provider's own: context length is num_ctx on
        # ollama and is not settable at all here, so nothing is translated.
        kw.update({k: v for k, v in (params or {}).items() if k not in kw})
        # Asked for explicitly: this dialect streams no usage at all unless it
        # is, and the context readout has nothing to show without it. Servers
        # that speak the format without supporting the flag are retried once
        # without it rather than losing the turn over a status line.
        kw.setdefault("stream_options", {"include_usage": True})
        try:
            stream = self._client().chat.completions.create(**kw)
        except Exception as e:
            if "stream_options" not in str(e): raise
            kw.pop("stream_options", None)
            stream = self._client().chat.completions.create(**kw)
        asm, order, last = CallAssembler(), [], None
        try:
            for chunk in stream:
                last = chunk
                if not chunk.choices:
                    yield Delta(raw=chunk)          # usage-only trailer on some servers
                    continue
                d = chunk.choices[0].delta
                for tc in getattr(d, "tool_calls", None) or []:
                    i = tc.index
                    if i not in order: order.append(i)
                    fn = getattr(tc, "function", None)
                    asm.start(i, getattr(fn, "name", "") or "", getattr(tc, "id", "") or "")
                    if fn and fn.arguments: asm.feed(i, fn.arguments)
                # openai's own models never return reasoning text, but several
                # compatible servers put it on one of these.
                think = getattr(d, "reasoning_content", None) or getattr(d, "reasoning", None) or ""
                content = getattr(d, "content", None) or ""
                if think or content: yield Delta(thinking=think, content=content, raw=chunk)
            # Arguments arrive as JSON fragments with no per-call stop event,
            # so a call can only be closed once the stream ends.
            done = [c for c in (asm.close(i) for i in order) if c]
            if done: yield Delta(tool_calls=[_openai_call(c) for c in done], raw=last)
        finally:
            stream.close()

    def models(self) -> list | None:
        """Whatever /v1/models returns. Every server speaking this dialect has
        the endpoint, and what is behind it ranges from one local model to
        openrouter's several hundred -- the picker filters, so the length is
        not this method's problem."""
        try: return sorted(m.id for m in self._client().models.list())
        except Exception: return None

    def capabilities(self, model: str) -> list | None:
        """What this model can do, when the endpoint publishes it.

        Only openrouter does, out of everything speaking this dialect: its
        catalog carries input_modalities per model, which is the same
        authoritative answer ollama gives and the same one nobody else offers.
        The direct openai and anthropic endpoints do not describe modality at
        all, and the aggregators (groq, together, nvidia, lmstudio) serve
        whatever they serve -- for those the answer comes the expensive way,
        from an image that gets rejected once and is remembered.

        None means "no answer", which is different from "no vision" and must
        stay different: an unreachable catalog has to leave the question open
        rather than blind a model that can see perfectly well.
        """
        if not self.base_url or "openrouter.ai" not in self.base_url: return None
        try:
            import httpx
            data = httpx.get("https://openrouter.ai/api/v1/models", timeout=10).json()["data"]
        except Exception: return None
        for m in data:
            if m.get("id") != model: continue
            arch, params = m.get("architecture") or {}, m.get("supported_parameters") or []
            caps = ["completion"]
            if "image" in (arch.get("input_modalities") or []): caps.append("vision")
            if "tools" in params: caps.append("tools")
            # Whether the effort knob does anything here. openrouter normalizes
            # reasoning across its catalog and lists it per model, so this is
            # the one openai-dialect endpoint that can be asked instead of
            # guessed at -- and a model without it gets no rung that lies.
            if {"reasoning", "reasoning_effort", "include_reasoning"} & set(params):
                caps.append("thinking")
            return caps
        return None                     # unlisted model: not an answer either

    def context_window(self, model: str) -> int:
        """The model's window, when the server publishes one, else 0.

        /v1/models is a fixed schema on paper and a grab bag in practice: the
        direct openai endpoint returns four fields and none of them is the
        window, while openrouter puts context_length on every row and the
        self-hosted servers -- vllm, sglang, lmstudio -- report the length they
        were actually launched with under max_model_len. The SDK keeps fields it
        does not know about, so all of them are reachable from the same call.

        Nothing here guesses. openai and anthropic simply do not say, and 0 is
        the honest answer for them.
        """
        # Short timeout, unlike every other call here: this one runs on the path
        # that reports what a round cost, and a status line is not worth making
        # anyone wait on a catalog endpoint that has stopped answering.
        try:
            c = self._client()
            m = (c.with_options(timeout=5.0) if hasattr(c, "with_options") else c).models.retrieve(model)
        except Exception: return 0
        extra = dict(getattr(m, "model_extra", None) or {})
        # openrouter states the catalog's number at the top level and the
        # serving provider's -- the one a request actually gets -- below it.
        for source in (extra.get("top_provider") or {}, extra):
            if not isinstance(source, dict): continue
            for key in ("context_length", "max_model_len", "max_context_length", "context_window"):
                n = source.get(key)
                if isinstance(n, int) and n > 0: return n
        return 0

    def assistant_message(self, thinking: str, content: str, tool_calls: list) -> dict:
        # Reasoning text is display-only here: chat-completions has no field to
        # send it back in. It still reaches disk, on the session record.
        msg = {"role": "assistant", "content": content or None}
        if tool_calls: msg["tool_calls"] = tool_calls
        return msg

    def user_message(self, text: str, images: list = None) -> dict:
        if not images: return {"role": "user", "content": text}
        # The text block is omitted when there is nothing in it. An attachment
        # sent with no words is an ordinary thing to do -- drag a picture in,
        # press enter -- and an empty string in a content block is a 400 here
        # and on anthropic both.
        return PinnedUser(role="user", content=[
            *([{"type": "text", "text": text}] if text else []),
            *({"type": "image_url", "image_url": {"url": f"data:{_media_type(b)};base64,{b}"}} for b in images)])

    def parse_calls(self, native: list) -> list:
        out = []
        for i, c in enumerate(native):
            fn = c.get("function", {})
            args = fn.get("arguments")
            if isinstance(args, str):
                # Unlike ollama, arguments cross the wire as a JSON string.
                try: args = json.loads(args) if args.strip() else {}
                except json.JSONDecodeError: args = {}
            out.append(ToolCall(c.get("id") or f"call_{i}", fn.get("name", ""), args or {}))
        return out

    def result_messages(self, pairs: list) -> list[dict]:
        """One tool message per result, plus a user message for any images.

        A tool message cannot carry an image in this dialect, and json.dumps of
        a screenshot result would inline ~700KB of base64 as plain text that
        the model cannot see as an image anyway.
        """
        out = []
        for call, result in pairs:
            data = (result or {}).get("result")
            images = _images(data)
            if not images:
                out.append({"role": "tool", "tool_call_id": call.id, "content": json.dumps(result)})
                continue
            meta = {k: v for k, v in data.items() if k not in ("image_base64", "images")}
            if not self.vision:
                # Reached only by replay: a blind model is never offered an
                # image-returning tool, but a session recorded on a sighted one
                # can still be reopened here, and the image would 400 the turn.
                out.append({"role": "tool", "tool_call_id": call.id, "content": json.dumps(
                    {"note": f"{len(images)} image(s) captured, not shown: this model cannot see images. "
                             "describe_image can have a sighted model answer one question about it",
                     **meta})})
                continue
            out.append({"role": "tool", "tool_call_id": call.id, "content": json.dumps(
                {"note": "images are attached as the next user message, not included here", **meta})})
            out.append({"role": "user", "content": [
                {"type": "text", "text": f"here are the {len(images)} photo(s) you requested"},
                *({"type": "image_url", "image_url": {"url": f"data:{_media_type(b)};base64,{b}"}} for b in images)]})
        return out

    def native_calls(self, calls: list[dict]) -> list[dict]:
        return [_openai_call({"id": c.get("id") or f"call_{i}", "name": c["name"], "args": c["args"]})
                for i, c in enumerate(calls)]

def _openai_call(c: dict) -> dict:
    return {"id": c["id"], "type": "function",
            "function": {"name": c["name"], "arguments": json.dumps(c["args"])}}