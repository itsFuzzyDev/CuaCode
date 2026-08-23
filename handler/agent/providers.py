import copy, json, os, socket
from dataclasses import dataclass, field
from urllib.parse import urlparse

import ollama

from tools._parser.FromProvider import ToolCall, parse_tool_calls
from tools._parser.ToProvider import format_tool_result

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
        # Ollama takes the system prompt as ordinary leading messages.
        # Anthropic and gemini take it as a request parameter instead, which
        # is why the caller hands it over separately rather than prepending it.
        # One message per segment: most chat templates render the message list
        # in a loop and reproduce all of them. A template that keeps only the
        # first system message would drop the later segments -- if a model
        # starts acting like it never read the environment block, that is the
        # thing to check.
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
        return {"role": "user", "content": text, "images": list(images)}

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
                # Same guard the other two dialects carry, and it was missing
                # here. Reached on replay: a conversation recorded on a model
                # that could see, reopened on one that cannot. Without it every
                # stored screenshot is rebuilt straight back into the history
                # and the next request fails -- and so does the one after,
                # because the images are still in the list.
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
        return {"role": "user", "content": [
            {"type": "text", "text": text},
            *({"type": "image_url", "image_url": {"url": f"data:{_media_type(b)};base64,{b}"}} for b in images)]}

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
        return {"role": "user", "content": [
            {"type": "text", "text": text},
            *({"type": "image", "source": {"type": "base64", "media_type": _media_type(b),
                                           "data": b}} for b in images)]}

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

def _openai_call(c: dict) -> dict:
    return {"id": c["id"], "type": "function",
            "function": {"name": c["name"], "arguments": json.dumps(c["args"])}}

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

def _serialize(tc):
    for attr in ("model_dump", "dict"):
        fn = getattr(tc, attr, None)
        if fn: return fn()
    return tc

# Registry key is the provider you pick in settings; the class behind it is
# whichever wire dialect that provider speaks.
PROVIDERS = {
    "ollama":     Ollama(),
    "openai":     OpenAI("gpt-5.6-sol"),
    "anthropic":  Anthropic(),
    "nvidia":     OpenAI("minimaxai/minimax-m3", "https://integrate.api.nvidia.com/v1", "NVIDIA_API_KEY"),
    "openrouter": OpenAI("poolside/laguna-s-2.1:free", "https://openrouter.ai/api/v1", "OPENROUTER_API_KEY", vision=False), # if you have money you can use better models. 
    "deepseek":   OpenAI("DeepSeek-V4-Flash-Vision-Exp", "https://api.deepseek.com", "DEEPSEEK_API_KEY", vision=True),
    "together":   OpenAI("MiniMaxAI/MiniMax-M3", "https://api.together.xyz/v1", "TOGETHER_API_KEY"),
    "qubrain":    OpenAI("glm-5.2", "https://qubrain.org/v1", "QB_API_KEY")
    # You can install local providers (including ollama local, litellm, all as long as they follow OpenAI schema )
    #- the Ollama class though is set to CLOUD ONLY, if youd like to set ollama local or local models you can use openai schema on the localhost )
}

def get(name: str):
    p = PROVIDERS.get(name)
    if p is None: raise ValueError(f"unknown provider: {name!r} (have {sorted(PROVIDERS)})")
    return p

# Instance state that must not be inherited by a copy, and what it resets to.
# The client is rebuilt rather than shared because it is cheap to rebuild and
# the key it was constructed with may not be this run's.
_FRESH = {"_c": None, "_thinking": [], "_key": "", "_host": ""}

def new(name: str):
    """An instance of the same provider that shares nothing mutable.

    The registry holds one instance per provider and they carry per-turn state
    on themselves -- Anthropic hands its thinking blocks from stream() to
    assistant_message() through self._thinking, and every class caches a
    client. That is fine while one loop runs at a time, and is a data race the
    moment two do: agent A's thinking blocks, signature and all, land in agent
    B's assistant message and the request is rejected for a signature that does
    not match the turn it is attached to.

    Copied rather than reconstructed because the openai class carries its
    identity in constructor arguments -- base_url, key_env, default_model,
    vision -- and there is no registry of what each entry was built with.
    """
    p = copy.copy(get(name))
    for attr, val in _FRESH.items():
        if hasattr(p, attr): setattr(p, attr, list(val) if isinstance(val, list) else val)
    return p
