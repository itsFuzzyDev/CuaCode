"""Agents as functions: a prompt in, a validated dict out.

There is no second agent loop here. generate() already drives a provider-
agnostic round loop taking its tools, system prompt and message list as
arguments, so a subagent is that loop pointed at a different system prompt with
a narrower tool set and a message list nobody keeps. What this module adds is a
schema the agent is made to fill and a definite answer about whether it did.

The forcing is a tool, not a provider's JSON mode: every dialect here speaks
tool calls already, whereas native structured output is spelled differently by
each provider and not at all by most of the openai-compatible servers. A
subagent starts cold, seeing only a rendered prompt string, which is what keeps
run() a function of its arguments.
"""
from contextvars import ContextVar
from dataclasses import dataclass, field

from tools.loader import Tool
from tools._parser.Validate import check
from handler import usage
from handler.agent import providers

SUBMIT = "submit_result"

# Tools that start another agent. Stripped from a subagent's tool list once the
# nesting limit is reached, so depth is bounded by what is offered rather than by
# asking a model not to recurse.
SPAWNERS = ("agent", "workflow")
MAX_DEPTH = 1

_depth = ContextVar("subagent_depth", default=0)

def depth() -> int:
    """How many agents deep the current call is. Zero in the main loop."""
    return _depth.get()

@dataclass
class AgentSpec:
    """What an agent is. Built inline by a caller or read off a file's
    frontmatter; the fields are the same either way."""
    name: str = "subagent"
    description: str = ""
    system: str = ""
    tools: list = field(default_factory=list)   # names; [] is genuinely no tools
    schema: dict = None                         # set => output is a validated dict
    provider: str = None                        # None inherits the active provider
    model: str = None
    effort: str = ""                            # "" inherits the conversation's level
    max_rounds: int = 8
    params: dict = None

SUBMIT_DESC = """Return your result to whoever asked for it.

This is the only channel back: the caller receives exactly the arguments of
this call and never sees the rest of your turn, so anything you write outside
it is discarded. Call it once, when you are done."""

APPENDIX = f"""

## Returning your result

Finish by calling `{SUBMIT}`. Its arguments are the entire result -- the caller
sees nothing else you write, so do not summarize your findings in prose as
well. If the material does not answer the question, say so *in the fields*
rather than leaving them empty or refusing to call it."""

def _submit_tool(schema: dict, box: dict) -> Tool:
    """The caller's schema, wearing a tool for a hat.

    Tool is a plain dataclass, so one built here is indistinguishable from one
    loaded off disk: to_provider() renders it into whatever dialect is in play
    and dispatch() validates and runs it without knowing this is not a real tool.
    """
    def handler(args: dict, ctx=None) -> dict:
        value, errs = check(args or {}, schema, defaults=True)
        if errs:
            # Back to the model as an ordinary tool result, which makes the next
            # round a correction: a retry loop for free, bounded by max_rounds.
            return {"ok": False, "errors": errs, "hint": f"fix these and call {SUBMIT} again"}
        box["value"] = value
        return {"ok": True}
    return Tool(name=SUBMIT, description=SUBMIT_DESC, input_schema=schema or {},
                output_schema={}, active=True, require_permissions=False, handler=handler)

def _effort(spec: AgentSpec, ctx) -> str:
    """The rung this agent runs at.

    A spec that names one means it: a cheap helper that only has to summarize a
    page should not burn the conversation's thinking budget because the user
    turned the dial up. Everything else inherits the conversation, the level the
    user actually chose; the account default is the last resort for a run with no
    conversation behind it.
    """
    from handler import config
    inherited = ctx.get("effort") if isinstance(ctx, dict) else ""
    return spec.effort or inherited or config.default_effort()

def _settings(spec: AgentSpec, ctx=None):
    """This run's provider settings. Read fresh rather than taken from the
    parent, because an agent may name a provider the conversation is not using,
    and then none of the conversation's model, params or vision flag apply."""
    from handler import config
    active = config.settings()
    provider = spec.provider or active["provider"]
    entry = config.entry(provider)
    out = {"provider": provider, "effort": _effort(spec, ctx)}
    # Inherited only for the same provider: the active model name is meaningless
    # to a different endpoint.
    model = spec.model or (active.get("model") if provider == active["provider"] else entry.get("model"))
    if model: out["model"] = model
    if "vision" in entry: out["vision"] = entry["vision"]
    if entry.get("effort_map"): out["effort_map"] = entry["effort_map"]
    # Per-model params follow the model the agent asked for, not the one the
    # conversation is on: that is the point of keying them by model.
    params = {**config.params_for(provider, model or config.model_for(provider)),
              **(spec.params or {})}
    if params: out["params"] = params
    return out, config.api_key(provider), provider

def _drive(spec, messages, settings, key, system, ctx, allow, extra, box, provider, budget):
    """One pass of the round loop. Returns (content, rounds, stopped).

    These chunks stop here on purpose. `ctx["emit"]` is the parent's own chunk
    outbox, so forwarding a subagent's round/assistant/done into it would have the
    parent's turn loop count its rounds and write its records. A subagent reports
    through the answer it returns, and a progress event would need its own wire.
    """
    from handler.agent.main import generate      # late: main imports the tool registry

    stop = ctx.get("cancelled") if isinstance(ctx, dict) else None
    if not callable(stop): stop = None
    gen = generate(API_KEY=key, ctx=ctx, messages=messages, settings=settings, system=system,
                   cancelled=stop, allow=allow, extra=extra,
                   # Never the shared singleton: it carries per-turn state that
                   # the parent conversation is in the middle of using.
                   provider_obj=providers.new(provider))
    rounds, content, stopped = 0, "", "no_calls"
    try:
        for chunk in gen:
            t = chunk.get("type")
            if t == "usage":
                # A side round is billed like any other, and the loop is the only thing
                # that writes meta.json, so it goes into the queue the loop drains rather
                # than onto a session it may no longer be the live one.
                usage.record(spec.name, chunk, provider)
            if t == "round":
                # Yielded before the request is made, so closing here spends
                # nothing. Budget is in rounds because that is what costs money.
                rounds += 1
                if rounds > budget:
                    stopped = "max_rounds"
                    break
            elif t == "assistant":
                content = chunk.get("content") or content
            elif t == "tool_output" and chunk.get("name") == SUBMIT and "value" in box:
                # generate() only ends a round loop when a round makes no calls,
                # so an agent that already answered would pay to say goodbye.
                stopped = "submitted"
                break
            elif t == "cancelled":
                stopped = "cancelled"
                break
            elif t == "failed":
                # The connection dropped mid-reply. Whatever arrived is already in
                # `content` and comes back with the agent, labelled so the caller
                # can tell it apart from an agent that finished.
                stopped = "connection_lost"
                break
            elif t == "done":
                stopped = "submitted" if "value" in box else "no_calls"
                break
    finally:
        gen.close()
    return content, rounds, stopped

def run(spec: AgentSpec, prompt: str, ctx=None, images: list = None) -> dict:
    """Run one agent to completion.

    Returns {output, rounds, stopped}. With a schema, output is the validated
    dict and `stopped` is "submitted" when it is trustworthy; without one, output
    is the agent's final text. An agent that finishes without answering comes back
    as an error rather than as prose in a field the caller expected to index.
    """
    d = _depth.get()
    if d >= MAX_DEPTH + 1:
        return {"error": "subagent nesting limit reached", "stopped": "error", "rounds": 0}

    settings, key, provider = _settings(spec, ctx)
    allow = (None if spec.tools is None
             else [t for t in spec.tools if not (d >= MAX_DEPTH and t in SPAWNERS)])
    box = {}
    extra = {SUBMIT: _submit_tool(spec.schema, box)} if spec.schema else None
    system = (spec.system or "") + (APPENDIX if spec.schema else "")
    # Shaped by the provider about to receive it, because an image in a user turn
    # is spelled three different ways. This is how a blind parent can hand a
    # subagent something to look at.
    messages = [providers.get(provider).user_message(prompt, images)]

    token = _depth.set(d + 1)
    from integrations.mcp import client as mcp_client
    scope_token = mcp_client.set_session_scope({})
    try:
        content, rounds, stopped = _drive(spec, messages, settings, key, system, ctx,
                                          allow, extra, box, provider, spec.max_rounds)
        # One nudge, once: a model that wrote its answer as text is a round away
        # from being right, and re-running the whole agent costs what it did.
        if spec.schema and "value" not in box and stopped == "no_calls" and rounds < spec.max_rounds:
            messages.append({"role": "user", "content":
                             f"You did not call {SUBMIT}, so nothing was returned. "
                             f"Call it now with what you found."})
            more, r2, stopped = _drive(spec, messages, settings, key, system, ctx,
                                       allow, extra, box, provider, 1)
            content, rounds = more or content, rounds + r2
    except Exception as e:
        # Named: two agents can fail with the same provider error, and the caller
        # has to be able to tell which one it paid for.
        return {"error": f"agent {spec.name}: {e}", "stopped": "error", "rounds": 0}
    finally:
        mcp_client.reset_session_scope(scope_token)
        _depth.reset(token)

    out = {"rounds": rounds, "stopped": stopped, "agent": spec.name}
    if not spec.schema:
        return {**out, "output": content}
    if "value" not in box:
        return {**out, "error": f"agent finished without calling {SUBMIT}", "text": content}
    return {**out, "output": box["value"]}
