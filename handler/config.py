import json, os
from pathlib import Path

from handler.agent import effort, providers
from handler.session import store

DEFAULTS = {"active": "ollama", "vision": "", "providers": {}}

def path() -> Path: return store.home() / "config.json"

def _seed() -> dict:
    """The file written on first run.

    Every registered provider gets a blank entry, so the config reads as a
    fill-in-the-blank template instead of a two-line stub. Blank is not the
    same as absent for the two keys seeded here -- settings() skips an empty
    model and api_key() falls through an empty key -- so a seeded entry
    behaves exactly like no entry until something is typed into it. vision is
    deliberately not seeded: it is the one field where a written value would
    override the provider's own, and openrouter's False has to survive.
    quirks and effort_map are not seeded either -- both are written only when
    something is learned or overridden, and absent means neither has happened.

    The reasoning effort *level* is not here at all: it belongs to a
    conversation, not to an account, so it lives in the session's meta.json
    next to provider and model. What stays here is the two things that are
    genuinely about the provider -- effort_map, your correction to where a
    rung sits, and quirks, what this endpoint has already refused.
    """
    return {"active": DEFAULTS["active"], "vision": "",
            "providers": {name: {"model": "", "api_key": "", "params": {}}
                          for name in sorted(providers.PROVIDERS)}}

def load() -> dict:
    """A missing or unparseable config seeds itself on disk rather than living
    only in memory, so the file is there to hand-edit from first boot instead
    of appearing once some setting happens to be changed through the UI."""
    cfg = store.read_json(path())
    return {**DEFAULTS, **cfg} if cfg else save(_seed())

def save(cfg: dict) -> dict:
    store.write_json(path(), cfg)
    # The file holds API keys in plaintext. 0600 is the whole protection, so
    # it is reapplied on every write rather than trusted to stick.
    os.chmod(path(), 0o600)
    return cfg

def entry(name: str, cfg: dict = None) -> dict:
    return ((cfg or load()).get("providers") or {}).get(name) or {}

def settings(cfg: dict = None) -> dict:
    """The settings dict generate() takes. Model is omitted when unset so the
    provider's own default applies rather than an empty string."""
    cfg = cfg or load()
    name = cfg.get("active") or "ollama"
    e = entry(name, cfg)
    out = {"provider": name}
    if e.get("model"): out["model"] = e["model"]
    # Always stated, never inferred downstream: this is where the written flag
    # and what the model has already refused are reconciled.
    out["vision"] = can_see(name, cfg)
    if e.get("effort_map"): out["effort_map"] = e["effort_map"]
    if e.get("params"): out["params"] = e["params"]
    return out

def api_key(name: str, cfg: dict = None) -> str:
    """Environment wins over the file, so a shell can override a stored key
    without editing anything."""
    key_env = getattr(providers.get(name), "key_env", "")
    return (os.environ.get(key_env) if key_env else "") or entry(name, cfg).get("api_key") or ""

# Substrings that mean "this model will not take a picture", as opposed to any
# of the other things a 400 can mean. Matched loosely because every endpoint
# words it differently and none of them use a machine-readable code for it.
_NO_IMAGE = ("does not support image", "not support image input", "no support for image",
             "image input is not", "does not support vision", "invalid_image",
             "image_url is not supported", "images are not supported")

def is_image_rejection(err: str) -> bool:
    e = (err or "").lower()
    return any(s in e for s in _NO_IMAGE)

def blind_models(name: str, cfg: dict = None) -> list[str]:
    """Models under this provider that have already refused an image."""
    return list(entry(name, cfg).get("blind_models") or [])

def learn_blind(name: str, model: str) -> dict:
    """Remember that this model cannot see.

    The `vision` flag is per provider, but vision is a property of the model:
    one ollama entry points at a vision model today and a text-only one
    tomorrow, and the registry default cannot know which. So it is learned the
    same way a rejected parameter is -- one failed request, once, ever -- and
    keyed by model so switching to a model that can see is not blocked by what
    the last one could not do.
    """
    if not model: return load()
    cfg = load()
    e = dict(entry(name, cfg))
    e["blind_models"] = sorted(set(e.get("blind_models") or []) | {model})
    cfg.setdefault("providers", {})[name] = e
    return save(cfg)

def model_for(name: str, cfg: dict = None) -> str:
    """The model this provider would actually use."""
    return entry(name, cfg).get("model") or getattr(providers.get(name), "default_model", "")

def model_caps(name: str, model: str, cfg: dict = None) -> list | None:
    """What a provider said this model can do, if it was ever asked."""
    caps = (entry(name, cfg).get("model_caps") or {}).get(model)
    return list(caps) if caps is not None else None

def learn_caps(name: str, model: str, caps: list) -> dict:
    if not model or caps is None: return load()
    cfg = load()
    e = dict(entry(name, cfg))
    e["model_caps"] = {**(e.get("model_caps") or {}), model: sorted(caps)}
    cfg.setdefault("providers", {})[name] = e
    return save(cfg)

# Providers already asked this process. The answer is on disk too; this only
# keeps a second process-lifetime call from happening before it gets there.
_asked = set()

def _ask_caps(name: str, model: str, cfg: dict = None) -> list | None:
    """Ask the provider what the model can do, once, if it can answer at all.

    Only ollama can. Everywhere else the answer comes the expensive way, from a
    rejected image, which is what learn_blind records. Failures are swallowed
    and nothing is written: a daemon that is down or a name that does not
    resolve must leave the question open rather than answer it wrongly.
    """
    p = providers.get(name)
    if not model or not hasattr(p, "capabilities"): return None
    if (name, model) in _asked: return None
    _asked.add((name, model))
    if not _reachable(name, cfg): return None
    p.setup(api_key(name, cfg))
    caps = p.capabilities(model)
    if caps is not None: learn_caps(name, model, caps)
    return caps

def can_see(name: str, cfg: dict = None, model: str = "") -> bool:
    """Whether this provider is expected to accept images.

    Four answers, in the order they deserve to be believed: what the user
    wrote, what the provider says about this exact model, what the model has
    already refused, and finally the registry default -- which is a guess about
    an endpoint, made before anyone knew which model it would be pointed at.

    model asks about one other than the configured one, which is what a vision
    helper with an override needs: the question is whether *that* model can see,
    not whether the one running the conversation can. The written flag is
    skipped in that case for the same reason -- it describes the entry, and this
    is a different model on it.
    """
    e = entry(name, cfg)
    if "vision" in e and not model: return bool(e["vision"])
    model = model or model_for(name, cfg)
    caps = model_caps(name, model, cfg)
    if caps is None: caps = _ask_caps(name, model, cfg)
    if caps is not None: return "vision" in caps
    if model in (e.get("blind_models") or []): return False
    return getattr(providers.get(name), "vision", True)

# Local-server probes, cached for the life of the process. Asking is cheap but
# not free, and this is read on every turn to build the environment block.
_probed = {}

def _reachable(name: str, cfg: dict = None) -> bool:
    """Whether a request to this provider could get anywhere.

    A key settles it. Otherwise the only providers left are the local ones, and
    for those "installed" is not "running" -- a base_url pointing at localhost
    with nothing listening behind it would otherwise be offered as the thing
    that can see, and named as such in the prompt, while every call to it fails
    on connect.
    """
    if api_key(name, cfg): return True
    url = getattr(providers.get(name), "base_url", "") or ""
    local = name == "ollama" or "localhost" in url or "127.0.0.1" in url
    if not local: return False
    if name not in _probed:
        _probed[name] = providers._daemon_up(url or None)
    return _probed[name]

def vision_helper(cfg: dict = None) -> tuple:
    """(provider, model) named to do the looking. ("", "") for auto.

    Two spellings, because the simple case should stay simple:

        "vision": "anthropic"
        "vision": {"provider": "ollama", "model": "minimax-m3:cloud"}

    The model override exists because the chat model and the model that looks
    at images are often the same provider and necessarily different models --
    running the conversation on a small text-only ollama model while a vision
    model on that same account does the looking is the normal case, not an
    exotic one. Without the override, naming that provider is impossible: its
    configured model is the blind one.
    """
    v = (cfg or load()).get("vision")
    if isinstance(v, dict):
        return (v.get("provider") or "").strip(), (v.get("model") or "").strip()
    return (v or "").strip(), ""

def set_vision_helper(name: str, model: str = None) -> dict:
    """Name the provider that looks at images, or "" to go back to auto.

    Checked here rather than at use time: a helper that cannot see or cannot be
    reached fails inside a tool call, hours later, as a puzzling error. Refusing
    it while someone is looking at the setting is the same information at a
    moment it is useful.
    """
    name, model = (name or "").strip(), (model or "").strip()
    if name:
        providers.get(name)               # raises on unknown, before anything is written
        if not _reachable(name): raise ValueError(f"{name} has no key and is not local")
        if not can_see(name, model=model):
            raise ValueError(f"{name}/{model or model_for(name)} cannot see images")
    cfg = load()
    cfg["vision"] = {"provider": name, "model": model} if (name and model) else name
    return save(cfg)

def lookers(cfg: dict = None) -> list[tuple]:
    """Every (provider, model) that could do the looking, best first.

    A pair rather than a name, because the named helper may carry a model
    override and the rest use whatever they are configured with. An empty model
    means "the one this provider already uses".
    """
    cfg = cfg or load()
    named, named_model = vision_helper(cfg)
    active = cfg.get("active") or "ollama"
    out, seen = [], set()
    for name, model in ([(named, named_model)] if named else []) + \
                       [(n, "") for n in [active] + sorted(providers.PROVIDERS)]:
        key = (name, model)
        if key in seen or name not in providers.PROVIDERS: continue
        seen.add(key)
        if can_see(name, cfg, model=model) and _reachable(name, cfg): out.append(key)
    return out

def sighted_all(cfg: dict = None) -> list[str]:
    """Provider names only, for callers that do not care which model."""
    return [n for n, _ in lookers(cfg)]

def sighted(cfg: dict = None) -> str:
    """A provider that can see and could be reached, or "".

    The point is running the agent on a model with no eyes: something still has
    to be able to look at a screenshot, and it does not have to be the model
    holding the conversation.

    A named helper wins, but only while it still holds up -- a key removed or a
    provider switched to vision:false must not leave the setting silently
    pointing at something that will fail. Otherwise the active provider is tried
    first, so a sighted setup never quietly farms the work out somewhere else.
    """
    found = lookers(cfg)
    return found[0][0] if found else ""

def use(name: str) -> dict:
    providers.get(name)          # raises on unknown, before anything is written
    cfg = load()
    cfg["active"] = name
    _WINDOWS.clear()             # the next turn asks the new provider its own size
    return save(cfg)

# Windows already established this process, keyed by provider and model. The
# provider is asked over the network, and the question comes up once per
# request; a table of model names is deliberately *not* what is cached here,
# because there is no such table anywhere in this file. Cleared whenever the
# provider or model changes, so a switch re-asks rather than reporting the
# previous model's window.
_WINDOWS: dict[tuple, int] = {}

def context_window(name: str, model: str, cfg: dict = None) -> int:
    """How many tokens this model's window holds, or 0 if nobody knows.

    Asked, never guessed. In order: what you wrote in config.json, the num_ctx
    the request itself sets, and then the provider's own answer -- ollama reads
    it off the daemon, the openai dialect reads it off /v1/models where the
    server publishes one, and anthropic does not publish it at all.

    0 is a real answer and not a failure. It means nothing authoritative was
    available, and a frontend showing "24k used" with no denominator is telling
    the truth, where one drawn against an invented total is not. Write
    context_window into the provider's entry to supply what the API will not.
    """
    e = entry(name, cfg)
    for n in (e.get("context_window"), (e.get("params") or {}).get("num_ctx")):
        if isinstance(n, int) and n > 0: return n

    key = (name, model)
    if key in _WINDOWS: return _WINDOWS[key]
    # Cached either way, including the zero: this runs on the path that reports
    # a round's token cost, and a provider that is slow to answer -- or not
    # answering at all -- must not be asked again on every turn.
    try:
        p = providers.get(name)
        p.setup(api_key(name, cfg))
        found = p.context_window(model or getattr(p, "default_model", ""))
    except Exception:
        found = 0
    _WINDOWS[key] = found if isinstance(found, int) and found > 0 else 0
    return _WINDOWS[key]

def quirks(name: str, model: str, cfg: dict = None) -> list[str]:
    """Request keys this provider/model pair has already had rejected once.

    Keyed by model rather than by provider: one registry entry gets pointed at
    a reasoning model today and a plain one tomorrow, and a rejection by the
    latter must not permanently mute the former.
    """
    return list(((entry(name, cfg).get("quirks") or {}).get(model) or []))

def learn_quirk(name: str, model: str, keys: list[str]) -> dict:
    """Record a rejection so no later turn sends that key to this model again.

    On disk rather than in memory, because the point is that an unrecognized
    model costs exactly one failed request ever, not one per boot.
    """
    if not keys: return load()
    cfg = load()
    e = dict(entry(name, cfg))
    q = dict(e.get("quirks") or {})
    q[model] = sorted(set(q.get(model) or []) | set(keys))
    e["quirks"] = q
    cfg.setdefault("providers", {})[name] = e
    return save(cfg)

def update(name: str, model: str = None, key: str = None, vision=None, params: dict = None,
           effort_map: dict = None) -> dict:
    providers.get(name)
    cfg = load()
    e = dict(entry(name, cfg))
    was = e.get("model")
    if model is not None: e["model"] = model
    if key is not None: e["api_key"] = key
    if vision is not None: e["vision"] = bool(vision)
    # Per-level remap, merged so fixing "high" does not wipe a "max" you set
    # last week. Keyed by canonical level; the value is native params, verbatim.
    if effort_map is not None:
        bad = [k for k in effort_map if k not in effort.LADDER]
        if bad: raise ValueError(f"unknown effort: {bad} (have {list(effort.LADDER)})")
        e["effort_map"] = {**(e.get("effort_map") or {}), **effort_map}
    # A model swap invalidates what was learned about the old one, and the new
    # one deserves its own first attempt.
    if model is not None and model != was:
        e.pop("quirks", None)
        e.pop("blind_models", None)
    # Merged, not replaced: setting num_ctx should not drop temperature.
    if params is not None: e["params"] = {**(e.get("params") or {}), **params}
    # Model, num_ctx and key all change what the window is or who would be
    # asked about it, so nothing learned about the old pairing survives.
    _WINDOWS.clear()
    cfg.setdefault("providers", {})[name] = e
    return save(cfg)

def listing(cfg: dict = None) -> list[dict]:
    """Everything the frontend needs to render a picker. Never the key itself:
    this crosses the IPC wire and ends up in logs."""
    cfg = cfg or load()
    active = cfg.get("active") or "ollama"
    out = []
    for name in sorted(providers.PROVIDERS):
        p, e = providers.get(name), entry(name, cfg)
        model = e.get("model") or p.default_model
        drop, override = quirks(name, model, cfg), e.get("effort_map")
        out.append({"name": name, "dialect": p.name, "active": name == active,
                    "model": model,
                    "base_url": getattr(p, "base_url", None),
                    "vision": e.get("vision", getattr(p, "vision", True)),
                    # The same five rungs everywhere, so the picker is one
                    # widget regardless of provider, plus what each rung would
                    # actually send to this model -- a slider whose effect is
                    # invisible is a slider nobody trusts, and it is also how
                    # you see a rung that quirks have quietly emptied out.
                    # The level itself is not here: it belongs to the session.
                    "effort_rungs": list(effort.LADDER),
                    "effort_map": override or {},
                    "effort_preview": {lvl: effort.resolve(name, p.name, model, lvl,
                                                           drop=drop, override=override)
                                       for lvl in effort.LADDER},
                    "params": e.get("params") or {},
                    "has_key": bool(api_key(name, cfg))})
    return out
