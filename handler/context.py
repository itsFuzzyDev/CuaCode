"""What the context window is actually full of.

Nobody can tell you exactly. A provider reports one number for the whole prompt
and never says which part of it cost what, so the parts here are measured in
characters, converted at a fixed rate, and then scaled so they add up to the one
number that was actually measured. That makes the shares trustworthy where the
absolute figures are not, which is the right way round: the question this
answers is "what is eating the window", not "how many tokens exactly is the
system prompt".

Images are the exception, and they are counted rather than estimated. An encoded
screenshot reads as a quarter of a million characters, so a chars-per-token rule
would report one photo as several windows' worth and drown every other category.
A picture is charged at a flat rate instead, which is far closer to what the
provider actually bills for one.

Nothing here asks the provider anything. It reads what is already loaded -- the
prompt, the tool schemas, the history in memory -- so /context costs no tokens
to answer, which would be a poor joke if it did.
"""
import json

# English prose lands near 4 characters per token, JSON and code a little under;
# the prompt is a mix of both. The exact rate barely matters once the parts are
# scaled to a measured total -- it decides the reading only for a conversation
# that has not sent a request yet.
CHARS_PER_TOKEN = 3.8

# What one image costs, near enough. Anthropic bills roughly width*height/750,
# which for a screenshot capped at its long edge lands here. A count of images is
# reported beside it, so a reading that looks wrong can at least be recomputed.
IMAGE_TOKENS = 1600

# Anything longer than this that looks like base64 is an encoded image rather
# than text. Real prose does not run a thousand characters without a space.
BLOB_CHARS = 1000

_B64 = set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/=-_")


def tokens_for_chars(n: int) -> int:
    return max(1, round(n / CHARS_PER_TOKEN)) if n else 0


def tokens(value) -> int:
    """An estimate for one string, or for anything JSON-shaped."""
    if not value: return 0
    if not isinstance(value, str): value = json.dumps(value, default=str)
    return tokens_for_chars(len(value))


def rate_fields(chunk: dict) -> dict:
    """A live rate, mid-round. Estimated, and says so in every field it sets.

    The phase is carried too, because the same number means different things in
    the two halves of a round: a hundred tokens a second while thinking is a
    model racing through a thought nobody will read, and the same figure while
    answering is the answer arriving.
    """
    out = {"phase": chunk.get("phase", ""), "tps_est": True}
    tps = chunk.get("tps") or 0
    if tps: out["tps"] = tps
    if chunk.get("phase") == "thinking":
        # Priced while it is still being written, so the thinking row stops being
        # the one part of a long wait with no number against it.
        out["thinking_tokens"], out["thinking_est"] = chunk.get("tokens", 0), True
        if tps: out["think_tps"] = tps
    elif tps:
        out["reply_tps"] = tps
    return out


def _rate_of(tokens: int, secs: float) -> float:
    """Tokens per second, or 0 for a span too short to divide by honestly."""
    return round(tokens / secs, 1) if tokens and secs >= 0.2 else 0.0


def turn_fields(chunk: dict) -> dict:
    """What the round that just generated cost, phase by phase.

    Thinking is reported by the provider where the provider reports it -- only
    the openai dialect breaks reasoning out -- and estimated from the text
    otherwise, marked as an estimate so nothing downstream presents a guess as a
    count. Anthropic bills thinking inside output_tokens and never itemises it,
    so the alternative to estimating is saying nothing at all about the part of
    the turn that took the longest.

    The reply is then whatever the round wrote that was not thinking: a
    subtraction where the split is known, an estimate where it is not. The two
    phases and their two clocks divide the round between them, so "where did the
    ninety seconds go" has an answer rather than one averaged rate that hides it.
    """
    usage = chunk.get("usage") or {}
    out, secs = {}, chunk.get("secs") or 0
    if n := usage.get("input"): out["in_tokens"] = n
    total = usage.get("output") or 0
    if total: out["out_tokens"] = total

    think_chars, reply_chars = chunk.get("thinking_chars") or 0, chunk.get("reply_chars") or 0
    if (n := usage.get("reasoning")) is not None:
        think = n
    elif total and (think_chars or reply_chars):
        # The provider billed one number for the whole reply and will not say
        # how much of it was thinking, so the measured total is divided where
        # the characters went. A split, not a guess bolted on: estimating each
        # half separately produced 147 tokens of thinking inside a 141-token
        # reply, which is the kind of arithmetic that makes a reader stop
        # believing every other number on the page.
        think = round(total * think_chars / (think_chars + reply_chars))
        out["thinking_est"] = True
    else:
        think = tokens_for_chars(think_chars)
        if think: out["thinking_est"] = True
    if total: think = min(think, total)
    if think: out["thinking_tokens"] = think

    if total:
        out["reply_tokens"] = total - think
        if out.get("thinking_est"): out["reply_est"] = True
    elif reply_chars:
        out["reply_tokens"] = tokens_for_chars(reply_chars)
        out["reply_est"] = True

    if r := _rate_of(think, chunk.get("thinking_secs") or 0): out["think_tps"] = r
    if r := _rate_of(out.get("reply_tokens", 0), chunk.get("reply_secs") or 0): out["reply_tps"] = r
    if r := _rate_of(total, secs):
        out["tps"], out["gen_secs"] = r, secs
    # Measured now, whatever was estimated a moment ago: a live rate still
    # marked as a guess after the provider has answered is the wrong kind of
    # honest, and the tilde on screen is how anyone tells the two apart.
    out["tps_est"] = False
    for key in ("thinking_secs", "reply_secs"):
        if chunk.get(key): out[key] = chunk[key]
    return out


def _is_blob(s: str) -> bool:
    if len(s) < BLOB_CHARS: return False
    if s.startswith("data:image"): return True
    head = s[:256]
    return all(c in _B64 for c in head)


def strip_images(value):
    """The same structure with encoded images taken out, and how many there were.

    Their bytes are worthless to a character count and ruinous to it, so they are
    removed before anything is measured and charged separately.
    """
    if isinstance(value, str):
        return ("", 1) if _is_blob(value) else (value, 0)
    if isinstance(value, dict):
        out, n = {}, 0
        for k, v in value.items():
            clean, found = strip_images(v)
            out[k], n = clean, n + found
        return out, n
    if isinstance(value, (list, tuple)):
        out, n = [], 0
        for v in value:
            clean, found = strip_images(v)
            out.append(clean)
            n += found
        return out, n
    return value, 0


def _count(fn) -> int:
    """A count from an integration, or zero. None of them is worth failing the
    readout over -- a missing number is a missing row, not an error."""
    try: return int(fn())
    except Exception: return 0


def _counts(cwd: str, app: str) -> dict:
    from integrations.mcp import loader as mcp
    from integrations.memory import loader as memory
    from integrations.skills import loader as skills
    from integrations.subagents import loader as subagents

    return {
        "skills": _count(lambda: len(skills.load_skills())),
        "memory": _count(lambda: len(memory.in_scope(path=cwd, apps=[app] if app else None))),
        "mcp": _count(lambda: len(mcp.load_servers())),
        "subagents": _count(lambda: len(subagents.load_agents())),
    }


# Which tools are big enough, and separate enough, to answer for themselves. Each
# of these carries an index of something that is loaded on demand -- the skills
# on disk, the memories in scope, the MCP servers registered -- and that index is
# the whole reason the thing is cheap. Folding them into one "tools" number would
# hide exactly the trade the design is making.
SIDECARS = {"skill": "skills", "memory": "memory", "mcp": "mcp"}

LABELS = {
    "system": "System prompt",
    "environment": "Environment",
    "tools": "Tool definitions",
    "skills": "Skills index",
    "memory": "Memory index",
    "mcp": "MCP index",
    "messages": "Messages",
    "images": "Images",
}


def _tool_parts(provider_name: str, vision: bool) -> tuple[dict, list, int]:
    """Every offered tool's schema, priced one at a time.

    Per tool rather than in one lump because the sidecars have to be pulled back
    out of the total, and because a toolbox that has quietly grown to a fifth of
    the window is a thing worth being able to see per row.
    """
    from handler.agent.main import registry, _visible
    from tools._parser.ToProvider import to_provider

    reg = {k: t for k, t in _visible(registry(), vision).items() if t.active}
    per, rows = {}, []
    for name, t in sorted(reg.items()):
        n = tokens(to_provider({name: t}, provider_name))
        per[name] = n
        rows.append({"name": t.name, "tokens": n})
    return per, rows, len(reg)


def report(sess, settings: dict, ctx=None, messages: list = None, last: dict = None) -> dict:
    """The whole readout, as one reply.

    measured is the prompt size the provider last charged for. When there is one
    the estimates are scaled to it, so the categories add up to something real;
    until then they are the estimates alone and say so.
    """
    from handler import config, environment
    from handler.agent import providers

    ctx = ctx or {}
    provider = settings.get("provider") or ""
    model = settings.get("model") or ""
    p = providers.get(provider)
    vision = settings.get("vision", getattr(p, "vision", True))

    per_tool, tool_rows, tool_count = _tool_parts(p.name, vision)
    sidecar = {key: per_tool.get(name, 0) for name, key in SIDECARS.items()}
    tools_left = sum(n for name, n in per_tool.items() if name not in SIDECARS)

    history = messages if messages is not None else sess.messages()
    clean, images = strip_images(history)

    parts = [
        {"key": "system", "tokens": tokens(sess.system)},
        {"key": "environment", "tokens": tokens(environment.block(ctx, settings, sess))},
        {"key": "tools", "tokens": tools_left, "count": tool_count - len(sidecar)},
        {"key": "skills", "tokens": sidecar["skills"]},
        {"key": "memory", "tokens": sidecar["memory"]},
        {"key": "mcp", "tokens": sidecar["mcp"]},
        {"key": "messages", "tokens": tokens(clean), "count": len(history)},
        # Charged flat, and never scaled with the rest: the reason it is a
        # separate category is that the character count everything else is
        # derived from is meaningless for it.
        {"key": "images", "tokens": images * IMAGE_TOKENS, "count": images, "flat": True},
    ]

    measured = int((last or {}).get("input") or 0)
    estimate = sum(part["tokens"] for part in parts)
    scaled = _calibrate(parts, measured, estimate)

    counts = _counts(str(ctx.get("cwd") or ""), str(ctx.get("frontmost_app") or ""))
    used = sum(part["tokens"] for part in parts)
    window = config.context_window(provider, model)

    for part in parts:
        part["label"] = LABELS.get(part["key"], part["key"])

    return {
        "provider": provider, "model": model, "window": window,
        "used": used, "free": max(window - used, 0) if window else 0,
        # What was estimated and what was charged, both said out loud: a readout
        # that quietly presented one as the other would be the misleading kind.
        "measured": measured, "estimated": estimate, "calibrated": scaled,
        "parts": [part for part in parts if part["tokens"] > 0 or part["key"] == "messages"],
        "sections": [
            {"key": "tools", "count": tool_count, "noun": "tool", "tokens": tools_left + sum(sidecar.values())},
            {"key": "skills", "count": counts["skills"], "noun": "skill", "tokens": sidecar["skills"],
             "hint": "bodies load on demand"},
            {"key": "memory", "count": counts["memory"], "noun": "memory in scope", "tokens": sidecar["memory"],
             "hint": "bodies load on demand"},
            {"key": "mcp", "count": counts["mcp"], "noun": "server", "tokens": sidecar["mcp"],
             "hint": "tools load on demand"},
            {"key": "subagents", "count": counts["subagents"], "noun": "subagent", "tokens": 0,
             "hint": "own context, not yours"},
        ],
        "tools": tool_rows,
        "last": dict(last or {}),
    }


def _calibrate(parts: list, measured: int, estimate: int) -> bool:
    """Scale the estimated parts onto the measured prompt, if there is one.

    Only the estimated ones: the flat-rate categories are already closer to the
    truth than anything a character count could say about them, and stretching
    them to close a gap would corrupt the one number here that is not a guess.
    """
    flat = sum(part["tokens"] for part in parts if part.get("flat"))
    room, base = measured - flat, estimate - flat
    if measured <= 0 or base <= 0 or room <= 0: return False
    factor = room / base
    # A wild factor means the two are not measuring the same thing -- a history
    # rebuilt for another provider, a window that has just been cleared -- and
    # scaling by it would turn a rough answer into a confident wrong one.
    if not 0.4 <= factor <= 2.5: return False
    for part in parts:
        if not part.get("flat"): part["tokens"] = round(part["tokens"] * factor)
    return True
