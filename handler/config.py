import json, os
from pathlib import Path

from handler.agent import effort, providers
from handler.session import store

# always_skills: names of skills whose bodies go into the system prompt at
# startup instead of waiting behind the skill tool. Seeded empty so the key is
# in the file to fill in, and so a bundled skill can be forced on without
# editing a file the next update overwrites. A skill can also ask for this
# itself with `always: true` in its own frontmatter.
DEFAULTS = {"active": "ollama", "vision": "", "providers": {}, "always_skills": [],
            "effort": ""}

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

    The reasoning effort level a conversation is *running* at is not here: it
    belongs to that conversation, so it lives in the session's meta.json next
    to provider and model. What is here is the level the next conversation
    starts on -- otherwise the setting dies with every restart and the answer
    to "what effort am I on" is "whatever the provider does by default", which
    is not a setting anyone chose. Per provider there are two more: effort_map,
    your correction to where a rung sits, and quirks, what this endpoint has
    already refused.
    """
    return {"active": DEFAULTS["active"], "vision": "", "always_skills": [], "effort": "",
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
    params = params_for(name, out.get("model") or model_for(name, cfg), cfg)
    if params: out["params"] = params
    return out

def default_effort(cfg: dict = None) -> str:
    """The rung a new conversation starts on. Blank means the provider's own
    default, which is what an account that has never set one gets."""
    level = ((cfg or load()).get("effort") or "").strip()
    return level if level in effort.LADDER else ""

def set_default_effort(level: str) -> dict:
    """Remember the rung, so the next session opens on it instead of on
    whatever the provider does when told nothing.

    Written whenever a level is chosen, rather than through a setting of its
    own: the choice has already been made by then, and a default that has to be
    set twice is a default nobody has set.
    """
    level = (level or "").strip()
    if level and level not in effort.LADDER:
        raise ValueError(f"unknown effort: {level!r} (have {list(effort.LADDER)})")
    cfg = load()
    if cfg.get("effort") == level: return cfg
    cfg["effort"] = level
    return save(cfg)

def api_key(name: str, cfg: dict = None) -> str:
    """Environment wins over the file, so a shell can override a stored key
    without editing anything.

    Last comes whatever the provider can find on this machine by itself: ollama
    is signed into through its desktop app, which leaves a usable key in
    ~/.ollama and means a working account can be configured nowhere at all.
    Lowest precedence, so pasting a key still overrides the machine's.
    """
    p = providers.get(name)
    key_env = getattr(p, "key_env", "")
    return ((os.environ.get(key_env) if key_env else "") or entry(name, cfg).get("api_key")
            or getattr(p, "stored_key", lambda: "")() or "")

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

# Params that configure how this pairing is *described*, not what is sent. Read
# by name where they are needed and stripped from everything bound for the wire.
LOCAL_KEYS = frozenset({"context_window"})

def local_param(name: str, model: str, key: str, cfg: dict = None):
    """A LOCAL_KEY for this provider/model pair, per-model over provider-wide."""
    e = entry(name, cfg)
    for src in ((e.get("model_params") or {}).get(model) or {}, e.get("params") or {}, e):
        if (v := src.get(key)) is not None: return v
    return None

def params_for(name: str, model: str, cfg: dict = None) -> dict:
    """The native params this provider/model pair should be sent.

    Two layers, because a provider entry has one model but a machine has
    several: `params` is everything this endpoint always wants (num_ctx for a
    local server, a base temperature), and `model_params[<model>]` is what only
    that one model wants, merged over it. Per-model wins on a key both set, and
    a model with no entry gets the provider's params unchanged -- so adding one
    model's quirk never changes what any other model is sent.

    Keyed by model rather than folded into `params` for the same reason quirks
    and blind_models are: switching models must not carry the last one's
    settings with it, and must not lose them either.

    LOCAL_KEYS are dropped on the way out. They live in the same two dicts
    because that is where a per-model setting belongs, but they describe the
    pairing rather than the request -- the openai dialect forwards params
    verbatim, and a key the API has never heard of costs a 400.
    """
    e = entry(name, cfg)
    merged = {**(e.get("params") or {}), **((e.get("model_params") or {}).get(model) or {})}
    return {k: v for k, v in merged.items() if k not in LOCAL_KEYS}


# Bumped whenever capabilities() learns to report something it did not before.
# An answer recorded under an older revision is not wrong, it is short: it was
# written by code that never looked for the new field, so an absent capability
# means "not asked about" rather than "not supported". Reading it as the latter
# is how a model permanently loses a knob it has -- the reason this counter
# exists is that "thinking" was added to the openrouter answer after entries
# had already been written without it.
CAPS_REV = 2

def model_caps(name: str, model: str, cfg: dict = None) -> list | None:
    """What a provider said this model can do, if it was ever asked -- and
    asked by a version of this code that looked for everything we read now."""
    e = entry(name, cfg)
    if (e.get("caps_rev") or {}).get(model, 0) < CAPS_REV: return None
    caps = (e.get("model_caps") or {}).get(model)
    return list(caps) if caps is not None else None

def learn_caps(name: str, model: str, caps: list) -> dict:
    if not model or caps is None: return load()
    cfg = load()
    e = dict(entry(name, cfg))
    e["model_caps"] = {**(e.get("model_caps") or {}), model: sorted(caps)}
    e["caps_rev"] = {**(e.get("caps_rev") or {}), model: CAPS_REV}
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

def can_think(name: str, model: str = "", cfg: dict = None) -> bool | None:
    """Whether this model has a thinking knob at all, or None if nobody knows.

    Three states, not two, and the third is the useful one: the effort table
    only defers to this when it is a definite no. An unasked provider -- which
    is most of them, since only ollama and openrouter publish the answer --
    leaves the table's own guess standing rather than having the ladder wiped
    by a question nobody answered.
    """
    model = model or model_for(name, cfg)
    caps = model_caps(name, model, cfg)
    if caps is None: caps = _ask_caps(name, model, cfg)
    return None if caps is None else "thinking" in caps

def effort_block(level: str, name: str = None, model: str = None, cfg: dict = None) -> str:
    """Why this rung cannot be set on the model in use, or "" if it can.

    Only "off" ever answers with a reason. The alternative -- accepting it and
    sending the lowest rung the model has -- makes the one setting whose whole
    promise is "do not think" the one setting that silently does not keep it.
    """
    cfg = cfg or load()
    name = name or cfg.get("active") or "ollama"
    model = model or model_for(name, cfg)
    p = providers.get(name)
    if effort.reachable(name, p.name, model, level, thinks=can_think(name, model, cfg),
                        override=entry(name, cfg).get("effort_map")):
        return ""
    return (f"{model or name} cannot stop thinking, so \"off\" would quietly mean its "
            f"lowest rung — pick that, or override effort_map[\"off\"] for {name}")

# Local-server probes, cached for the life of the process. Asking is cheap but
# not free, and this is read on every turn to build the environment block.
_probed = {}

def _reachable(name: str, cfg: dict = None) -> bool:
    """Whether a request to this provider could get anywhere.

    A key settles it, and for ollama that includes the one its desktop app
    leaves behind. Otherwise the only providers left are the local ones, and
    for those "installed" is not "running" -- a base_url pointing at localhost
    with nothing listening behind it would otherwise be offered as the thing
    that can see, and named as such in the prompt, while every call to it fails
    on connect.
    """
    if api_key(name, cfg): return True
    url = getattr(providers.get(name), "base_url", "") or ""
    local = "localhost" in url or "127.0.0.1" in url
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
    context_window into the provider's entry to supply what the API will not --
    into `model_params[<model>]` when one entry gets pointed at several models,
    since the window belongs to the model and not to the endpoint.
    """
    # Resolved here rather than trusted from the caller: settings() omits the
    # model when it is unset, and a per-model window keyed by "" matches nothing.
    model = model or model_for(name, cfg)
    # local_param, not e["context_window"]: per-model first, then the entry-wide
    # value, so switching models does not report the last one's window.
    # params_for, not e["params"]: a num_ctx set for this model alone is the
    # window of the request that will actually be made.
    for n in (local_param(name, model, "context_window", cfg),
              params_for(name, model, cfg).get("num_ctx")):
        if isinstance(n, int) and n > 0: return n

    key = (name, model)
    if key in _WINDOWS: return _WINDOWS[key]
    # Cached either way, including the zero: this runs on the path that reports
    # a round's token cost, and a provider that is slow to answer -- or not
    # answering at all -- must not be asked again on every turn.
    try:
        p = providers.get(name)
        p.setup(api_key(name, cfg))
        found = p.context_window(model)
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
           effort_map: dict = None, model_params: dict = None) -> dict:
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
    # Same merge, one model deep: {"gpt-5.6-mini": {"temperature": 0}} touches
    # that model's params and leaves every other model's alone. A null value
    # removes an override rather than setting the key to nothing -- there is no
    # other way to say "let the provider-wide value through again". A model left
    # with no overrides drops out, so the file does not collect empty rows for
    # every model ever touched.
    if model_params is not None:
        mp = dict(e.get("model_params") or {})
        for mname, vals in model_params.items():
            row = dict(mp.get(mname) or {})
            for k, v in (vals or {}).items():
                if v is None: row.pop(k, None)
                else: row[k] = v
            if row: mp[mname] = row
            else: mp.pop(mname, None)
        if mp: e["model_params"] = mp
        else: e.pop("model_params", None)
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
        # Stored caps only: listing walks every provider, and a picker opening
        # is no reason to go and ask each of them a question over the network.
        caps = model_caps(name, model, cfg)
        thinks = None if caps is None else "thinking" in caps
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
                                                           drop=drop, override=override, thinks=thinks)
                                       for lvl in effort.LADDER},
                    # Whether the bottom rung means what it says here. It is
                    # the one rung that can be a lie rather than an
                    # approximation, so the frontend is told rather than left
                    # to read it out of the preview.
                    "effort_off": effort.reachable(name, p.name, model, "off",
                                                   thinks=thinks, override=override),
                    "params": e.get("params") or {},
                    # What this entry's own model is sent on top of `params`.
                    # The whole map, not just the active model's row: a picker
                    # that hides the other rows makes them look unset.
                    "model_params": e.get("model_params") or {},
                    "has_key": bool(api_key(name, cfg))})
    return out
