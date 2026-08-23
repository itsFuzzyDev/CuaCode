"""Agents as functions: a prompt in, a validated dict out.

There is no second agent loop here. generate() already drives a provider-
agnostic round loop and takes its tools, system prompt and message list as
arguments, so a subagent is that same loop pointed at a different system
prompt with a narrower tool set and a message list nobody keeps. What this
module adds is the part that makes the result callable code rather than prose:
a schema the agent is made to fill, and a definite answer about whether it did.

The forcing is done with a tool, not with a provider's JSON mode. Every
dialect here already speaks tool calls -- they are normalized by
CallAssembler, parse_calls and to_provider before anything reaches the loop --
whereas native structured output is spelled differently by each of the three
and not at all by most of the openai-compatible servers in PROVIDERS. One path
that works everywhere beats three that each work once.

A subagent starts cold: it never sees the parent's messages, only a rendered
prompt string. That is what keeps run() a function -- same arguments, same
work, no dependence on what the caller happened to be doing.
"""
from contextvars import ContextVar
from dataclasses import dataclass, field

from tools.loader import Tool
from tools._parser.Validate import check
from handler.agent import providers

SUBMIT = "submit_result"

# Tools that start another agent. Stripped from a subagent's tool list once
# the nesting limit is reached, so depth is bounded by what is offered rather
# than by asking a model not to recurse.
SPAWNERS = ("agent", "workflow")
MAX_DEPTH = 1

_depth = ContextVar("subagent_depth", default=0)

def depth() -> int:
    """How many agents deep the current call is. Zero in the main loop."""
    return _depth.get()

@dataclass
class AgentSpec:
    """What an agent is. Built inline by a caller, or read off a file's
    frontmatter -- the fields are the same either way, which is why a
    file-backed agent registry needs nothing from this module but a parser."""
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
    and dispatch() validates and runs it, both without knowing this is not a
    real tool. Nothing downstream learns the word "schema".
    """
    def handler(args: dict, ctx=None) -> dict:
        value, errs = check(args or {}, schema, defaults=True)
        if errs:
            # Back to the model as an ordinary tool result, which makes the
            # next round a correction. A retry loop for free, bounded by
            # max_rounds like everything else.
            return {"ok": False, "errors": errs, "hint": f"fix these and call {SUBMIT} again"}
        box["value"] = value
        return {"ok": True}
    return Tool(name=SUBMIT, description=SUBMIT_DESC, input_schema=schema or {},
                output_schema={}, active=True, require_permissions=False, handler=handler)

def _effort(spec: AgentSpec, ctx) -> str:
    """The rung this agent runs at.

    A spec that names one means it: a cheap helper that only has to summarize a
    page should not start burning the conversation's thinking budget because
    the user turned the dial up. Everything else inherits the conversation --
    which is the level the user actually chose, and the one thing a subagent
    that says nothing about effort should be doing. The account default is the
    last resort, for a run with no conversation behind it at all.
    """
    from handler import config
    inherited = ctx.get("effort") if isinstance(ctx, dict) else ""
    return spec.effort or inherited or config.default_effort()

def _settings(spec: AgentSpec, ctx=None):
    """This run's provider settings. Read fresh rather than taken from the
    parent: an agent is allowed to name a provider the conversation is not
    using, and then none of the conversation's model, params or vision flag
    apply to it."""
    from handler import config
    active = config.settings()
    provider = spec.provider or active["provider"]
    entry = config.entry(provider)
    out = {"provider": provider, "effort": _effort(spec, ctx)}
    # Inherited only when it is the same provider -- the active model name is
    # meaningless to a different endpoint.
    model = spec.model or (active.get("model") if provider == active["provider"] else entry.get("model"))
    if model: out["model"] = model
    if "vision" in entry: out["vision"] = entry["vision"]
    if entry.get("effort_map"): out["effort_map"] = entry["effort_map"]
    # Per-model params follow the model the agent asked for, not the one the
    # conversation is on: a spec that names a small model gets that model's
    # settings, which is the whole point of keying them by model.
    params = {**config.params_for(provider, model or config.model_for(provider)),
              **(spec.params or {})}
    if params: out["params"] = params
    return out, config.api_key(provider), provider

def _emit(ctx, name: str, chunk: dict):
    """Progress out to whatever the caller wired up, if anything. A frontend
    that raises in its own callback must not take the run down with it."""
    fn = ctx.get("emit") if isinstance(ctx, dict) else None
    if not callable(fn): return
    try: fn(name, chunk)
    except Exception: pass

def _drive(spec, messages, settings, key, system, ctx, allow, extra, box, provider, budget):
    """One pass of the round loop. Returns (content, rounds, stopped)."""
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
            _emit(ctx, spec.name, chunk)
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
                # so an agent that has already answered would otherwise keep
                # going and pay for another round to say goodbye.
                stopped = "submitted"
                break
            elif t == "cancelled":
                stopped = "cancelled"
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
    dict and `stopped` is "submitted" when it is trustworthy; without one,
    output is the agent's final text. An agent that finishes without answering
    comes back as an error rather than as prose in a field the caller expected
    to index -- a wrong shape is worse than a missing one, because only one of
    them is noticed.
    """
    d = _depth.get()
    if d >= MAX_DEPTH + 1:
        return {"error": "subagent nesting limit reached", "stopped": "error", "rounds": 0}

    settings, key, provider = _settings(spec, ctx)
    allow = [t for t in (spec.tools or []) if not (d >= MAX_DEPTH and t in SPAWNERS)]
    box = {}
    extra = {SUBMIT: _submit_tool(spec.schema, box)} if spec.schema else None
    system = (spec.system or "") + (APPENDIX if spec.schema else "")
    # Shaped by the provider that is about to receive it, because an image in a
    # user turn is spelled three different ways. This is how a subagent can be
    # handed something to look at when the model that called it cannot see.
    messages = [providers.get(provider).user_message(prompt, images)]

    token = _depth.set(d + 1)
    from integrations.mcp import client as mcp_client
    scope_token = mcp_client.set_session_scope({})
    try:
        content, rounds, stopped = _drive(spec, messages, settings, key, system, ctx,
                                          allow, extra, box, provider, spec.max_rounds)
        # One nudge, once. A model that ran out of tools to call and wrote its
        # answer as text is a round away from being right, and re-running the
        # whole agent to find that out again costs everything it already did.
        if spec.schema and "value" not in box and stopped == "no_calls" and rounds < spec.max_rounds:
            messages.append({"role": "user", "content":
                             f"You did not call {SUBMIT}, so nothing was returned. "
                             f"Call it now with what you found."})
            more, r2, stopped = _drive(spec, messages, settings, key, system, ctx,
                                       allow, extra, box, provider, 1)
            content, rounds = more or content, rounds + r2
    except Exception as e:
        return {"error": str(e), "stopped": "error", "rounds": 0}
    finally:
        mcp_client.reset_session_scope(scope_token)
        _depth.reset(token)

    out = {"rounds": rounds, "stopped": stopped, "agent": spec.name}
    if not spec.schema:
        return {**out, "output": content}
    if "value" not in box:
        return {**out, "error": f"agent finished without calling {SUBMIT}", "text": content}
    return {**out, "output": box["value"]}
