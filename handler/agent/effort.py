"""One reasoning-effort knob, translated per (provider, dialect, model).

Providers disagree on three axes at once, which is why a single passthrough
parameter was never going to cover it: the field's name and shape
(reasoning_effort vs thinking vs think vs reasoning vs enable_thinking), how
many rungs it has (grok has two, most of openai three, anthropic five), and
whether "off" is even reachable (fable-5 cannot stop thinking, opus-5 only
stops at effort high or below). A table settles all three per model, so
nothing downstream ever asks which provider it is talking to.

What the table does not settle is calibration. Where a rung sits on one
provider is an informed placement, not a measurement, and one provider's
"high" is not another's -- see _snap for which properties actually hold, and
resolve's `override` for the way out when a placement is wrong for you.
"""

import re
from dataclasses import dataclass

LADDER = ("off", "low", "medium", "high", "max")

# Keys this module owns, and the only ones it will strip after a rejection. A
# 400 on max_tokens is the caller's problem; a 400 on reasoning_effort is ours.
OWNED = ("reasoning_effort", "reasoning", "thinking", "output_config",
         "think", "enable_thinking", "thinking_budget")

@dataclass(frozen=True)
class Spec:
    shape: str                                  # translator, keyed into SHAPES
    rungs: tuple = ("low", "medium", "high")    # native ladder, ascending
    off: dict | None = None                     # params meaning "do not think";
                                                # None = this model cannot stop

def _snap(level: str, rungs: tuple) -> str:
    """Canonical rung -> the rung this model actually has.

    Name first: low/medium/high are near-universal, and "high" quietly landing
    on medium because the arithmetic said so is the kind of surprise that makes
    the whole control untrustworthy. Only a level the model has no word for --
    "max" on a three-rung ladder, "medium" on grok's two -- falls through to
    proportional placement, and that is a genuine guess, which is why
    Spec.rungs is data and config can override the result outright.

    Two properties hold either way, and they are the ones worth relying on:
    the mapping is monotonic (a higher canonical rung never yields a lower
    native one) and total (every level resolves, nothing raises). Parity across
    providers is not among them -- anthropic's "high" and openai's "high" are
    not the same amount of thinking, and no table can make them one.

    rungs holds thinking levels only. An off-ish native value like openai's
    "none" belongs in Spec.off; leaving it in the ladder shifts every rung down
    by one and quietly turns "low" into no thinking at all.
    """
    if level in rungs: return level
    i = LADDER.index(level)
    return rungs[round((i - 1) * (len(rungs) - 1) / (len(LADDER) - 2))]

# Share of max_tokens to hand to a provider that budgets thinking in tokens
# instead of naming levels.
_FRAC = {"low": .15, "medium": .35, "high": .6, "max": .9}

def _openai(level, spec, ctx):
    return {"reasoning_effort": _snap(level, spec.rungs)}

def _openrouter(level, spec, ctx):
    # extra_body, not a top-level kwarg: openai-python validates its signature
    # and raises TypeError on anything it does not declare, and `reasoning` is
    # openrouter's own extension. Same reason _qwen nests below.
    return {"extra_body": {"reasoning": {"effort": _snap(level, spec.rungs)}}}

def _anthropic(level, spec, ctx):
    # display=summarized because the thinking text is streamed to the UI; the
    # default omits it and the frontend just sees a long pause.
    return {"thinking": {"type": "adaptive", "display": "summarized"},
            "output_config": {"effort": _snap(level, spec.rungs)}}

def _anthropic_budget(level, spec, ctx):
    # 4.5 and older: no effort parameter, only a token budget, and it has to be
    # strictly under max_tokens. The 64000 mirrors the provider's own default,
    # so the two stay in step when neither is set explicitly.
    cap = ctx.get("max_tokens", 64000)
    return {"thinking": {"type": "enabled",
                         "budget_tokens": max(1024, int(cap * _FRAC[_snap(level, spec.rungs)]))}}

def _ollama(level, spec, ctx):
    return {"think": _snap(level, spec.rungs)}

def _ollama_bool(level, spec, ctx):
    # Most ollama models take a bare bool; only the gpt-oss family reads levels.
    return {"think": True}

def _qwen(level, spec, ctx):
    cap = ctx.get("max_tokens", 32000)
    return {"extra_body": {"enable_thinking": True,
                           "thinking_budget": int(cap * _FRAC[_snap(level, spec.rungs)])}}

def _none(level, spec, ctx):
    return {}                                   # thinking is not configurable here

SHAPES = {"openai": _openai, "openrouter": _openrouter, "anthropic": _anthropic,
          "anthropic_budget": _anthropic_budget, "ollama": _ollama,
          "ollama_bool": _ollama_bool, "qwen": _qwen, "none": _none}

# (provider_key, dialect, model_regex, Spec). First match wins, so the narrow
# rules sit above the broad ones. A None field means "any".
RULES = [
    # Registry-wide overrides first: openrouter normalizes reasoning across its
    # whole catalog, so the model behind it never gets consulted.
    ("openrouter", None, None,
     Spec("openrouter", off={"extra_body": {"reasoning": {"enabled": False}}})),

    # anthropic. Every quirk that would otherwise be a 400 is encoded in `off`.
    (None, "anthropic", r"^claude-(fable|mythos)-5",
     Spec("anthropic", ("low", "medium", "high", "xhigh", "max"), off=None)),
    (None, "anthropic", r"^claude-opus-5",
     # disabled thinking is accepted only at effort high or below, so off has
     # to pin the effort down with it or the pair is rejected.
     Spec("anthropic", ("low", "medium", "high", "xhigh", "max"),
          off={"thinking": {"type": "disabled"}, "output_config": {"effort": "high"}})),
    (None, "anthropic", r"^claude-(opus-4-[78]|sonnet-5)",
     Spec("anthropic", ("low", "medium", "high", "xhigh", "max"),
          off={"thinking": {"type": "disabled"}})),
    (None, "anthropic", r"^claude-(opus|sonnet)-4-6",
     Spec("anthropic", ("low", "medium", "high", "max"),
          off={"thinking": {"type": "disabled"}})),
    (None, "anthropic", r"^claude-",             # 4.5 and older: budget only
     Spec("anthropic_budget", ("low", "medium", "high", "max"),
          off={"thinking": {"type": "disabled"}})),

    # ollama: gpt-oss reads named levels, everything else a bare bool.
    (None, "ollama", r"gpt-oss", Spec("ollama", off={"think": False})),
    (None, "ollama", None,       Spec("ollama_bool", off={"think": False})),

    # The openai dialect, whichever host happens to be serving it.
    # "none" and "minimal" are off-values, not rungs -- see _snap.
    (None, "openai", r"^gpt-5\.1",
     Spec("openai", ("low", "medium", "high"), off={"reasoning_effort": "none"})),
    (None, "openai", r"^gpt-5",
     Spec("openai", ("low", "medium", "high"), off={"reasoning_effort": "minimal"})),
    (None, "openai", r"^o[134]",   Spec("openai", ("low", "medium", "high"))),
    (None, "openai", r"gpt-oss",   Spec("openai", ("low", "medium", "high"))),
    (None, "openai", r"^grok-4",   Spec("none")),        # not settable at all
    (None, "openai", r"^grok",     Spec("openai", ("low", "high"))),
    (None, "openai", r"^qwen3",    Spec("qwen", off={"extra_body": {"enable_thinking": False}})),
    (None, "openai", r"minimax",   Spec("none")),        # thinking always on
]

# Everything unmatched. Optimistic on purpose: the dialect's usual spelling is
# tried once, and learn() demotes the model for good if the server says no.
FALLBACK = {"openai": Spec("openai"), "anthropic": Spec("anthropic"),
            "ollama": Spec("ollama_bool", off={"think": False})}

def spec_for(provider: str, dialect: str, model: str) -> Spec:
    for p, d, pat, s in RULES:
        if p and p != provider: continue
        if d and d != dialect: continue
        if pat and not re.search(pat, model or "", re.I): continue
        return s
    return FALLBACK.get(dialect, Spec("none"))

def _without(native: dict, drop) -> dict:
    """Native params minus keys already known to be rejected, at either level.

    extra_body is a bag rather than a value, so a key inside it has to be
    reachable by name too -- that is where the openrouter and qwen knobs live.
    """
    drop = set(drop or ())
    if not drop: return native
    out = {k: v for k, v in native.items() if k not in drop}
    body = {k: v for k, v in (out.get("extra_body") or {}).items() if k not in drop}
    if "extra_body" in out:
        out["extra_body"] = body
        if not body: out.pop("extra_body")
    return out

def resolve(provider: str, dialect: str, model: str, level: str,
            params: dict | None = None, drop=None, override: dict | None = None) -> dict:
    """Canonical level -> native request params, merged under the caller's own.

    Hand-written params always win: the table is a default, not a cage. An
    unreachable level degrades instead of raising -- "off" on a model that
    cannot stop thinking becomes its lowest rung, which is the honest answer.

    override is the per-provider remap out of config, keyed by canonical level:
    {"high": {"reasoning_effort": "medium"}} replaces what the table would have
    produced for "high" on this provider, outright. It exists because the
    table's placements are informed choices, not measurements -- if a
    provider's "high" burns more than you want, that is a fact about that
    provider that belongs in your config rather than in an argument with this
    file. A level absent from override falls through to the table as usual.
    """
    params = dict(params or {})
    if not level or level not in LADDER: return params
    if override and level in override:
        native = dict(override[level] or {})
    else:
        s = spec_for(provider, dialect, model)
        if level == "off":
            native = dict(s.off) if s.off is not None else SHAPES[s.shape]("low", s, params)
        else:
            native = SHAPES[s.shape](level, s, params)
    native = _without(native, drop)
    merged = {**native, **params}
    # extra_body is a bag: a caller who sets one of their own should not
    # silently delete the resolver's.
    if "extra_body" in native and "extra_body" in params:
        merged["extra_body"] = {**native["extra_body"], **params["extra_body"]}
    return merged

def learn(err: str, params: dict) -> tuple[dict, list[str]] | None:
    """(params minus the key the server just rejected, the keys dropped).

    None when the rejection was not about anything this module put there, so
    the caller re-raises rather than papering over an unrelated 400. The test
    is a substring match against the error text -- providers name the offending
    field, and a false positive costs one silently dropped knob, not a failure.
    """
    named = [k for k in OWNED if k in (err or "")]
    if not named: return None
    out = dict(params or {})
    body = dict(out.get("extra_body") or {})
    dropped = []
    for k in named:
        # Both levels, not the first hit: `or` would short-circuit and leave a
        # copy behind in extra_body.
        hit = k in out or k in body
        out.pop(k, None)
        body.pop(k, None)
        if hit: dropped.append(k)
    if not dropped: return None
    if "extra_body" in out:
        out["extra_body"] = body
        if not body: out.pop("extra_body")
    return out, dropped
