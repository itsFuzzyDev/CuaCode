from tools._parser.FromProvider import ToolCall
from handler.agent import providers

def to_messages(records: list[dict], provider: str, native_ok: bool = True,
                origin: str = None, vision: bool = None) -> list[dict]:
    """Records -> provider messages, by running the same adapters the live
    loop runs. No second formatting path exists, so a reload cannot drift
    from what the agent actually sent the first time.

    Tool results are flushed a round at a time rather than one per record,
    matching the loop: anthropic and gemini require every result from a round
    in a single message.

    Stored provider-native tool_calls are reused only for records the target
    provider actually wrote, which is what makes a same-provider reload
    byte-identical; everything else is rebuilt from the canonical calls.
    origin covers records written before per-record stamping. native_ok=False
    forces the rebuild path for every record.
    """
    # Its own instance, with vision set from the config rather than from the
    # registry default. result_messages decides whether to re-inflate stored
    # images off p.vision, and the singleton's copy of that is whatever the
    # class was written with -- so a conversation reopened on a model that
    # cannot see would rebuild every screenshot straight back into the history
    # and fail on the next request, permanently. Never the shared instance:
    # this is a fact about one conversation, not about the provider.
    from handler import config          # imported late: config imports providers
    p = providers.new(provider)
    p.vision = config.can_see(provider) if vision is None else vision
    origin = origin or provider
    msgs, calls, i, pending = [], [], 0, []

    def flush():
        nonlocal pending
        if pending:
            msgs.extend(p.result_messages(pending))
            pending = []

    for rec in records:
        t = rec.get("t")
        if t == "user":
            flush()
            msgs.append({"role": "user", "content": rec.get("text", "")})
        elif t == "assistant":
            flush()
            wrote_it = (rec.get("p") or origin) == provider
            native = rec.get("native") if native_ok and wrote_it else None
            if native is None: native = p.native_calls(rec.get("calls", []))
            msgs.append(p.assistant_message(rec.get("thinking", ""), rec.get("content", ""), native))
            calls, i = p.parse_calls(native), 0
        elif t == "tool":
            # Call ids are positional in every parser we have, so replaying
            # the calls in order regenerates the exact ids the tools ran under.
            call = calls[i] if i < len(calls) else ToolCall(f"call_{i}", rec.get("name", ""), {})
            i += 1
            pending.append((call, rec.get("result", {})))
    flush()
    return msgs
