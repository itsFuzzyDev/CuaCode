import copy, json, os
from pathlib import Path

from handler import faults
from handler.agent import effort, providers
from handler.session import store

# always_skills: skills forced into the system prompt at startup rather than behind the
# skill tool, so an update cannot overwrite the choice. `always: true` also works.
DEFAULTS = {"active": "ollama", "vision": "", "providers": {}, "always_skills": [],
            "effort": ""}

# The four permission modes, in the order a picker shows them. One list: the config
# reader validates against it, and so does the wire's permission.mode -- which had
# its own copy of the four names, so a fifth mode would have been accepted by one
# and rewritten to "ask" by the other.
MODES = ("ask", "auto", "bypass", "off")

def path() -> Path: return store.home() / "config.json"

# The last file read, and the stamp it was read at. See load().
_cache: dict = {}

def blank() -> dict:
    """Everything a config can hold, at the value each reader already assumes.

    Written on first run and filled into a file that already exists, so the file
    reads as the whole set of options rather than only the ones somebody touched:
    a setting nobody knew about is a setting nobody uses.

    Blank is not absent, and every value here is what the code does when the key is
    missing -- `permission: {"mode": "ask"}` is the mode an empty config runs,
    `steps: {"provider": ""}` still falls back to vision, `compact: null` is the
    automatic ratio. That is what makes writing them in a no-op for behaviour.

    Three things are deliberately not here: `vision` inside a provider entry, which
    is consulted *instead of* the model's own capabilities when no model is named
    (so a seeded `true` would claim a blind model can see); and `quirks` and
    `effort_map`, which are learned by asking the endpoint and mean nothing as
    blanks.
    """
    return {
        "active": DEFAULTS["active"],
        "effort": "",
        "vision": {"provider": "", "model": ""},
        "steps": {"provider": "", "model": ""},
        "permission": {"mode": "ask", "provider": "", "model": ""},
        "compact": None,
        "always_skills": [],
        "providers": {name: {"model": "", "api_key": "", "params": {}, "model_params": {}}
                      for name in sorted(providers.PROVIDERS)},
    }

# The settings that take two spellings: a bare string, or the object that says more.
# `vision: "ollama"` names the looking provider and nothing about which model looks.
_SIDES = {"vision": "provider", "steps": "provider", "permission": "mode"}

def _spelled(cfg: dict) -> tuple[dict, bool]:
    """The two-spelling settings as the object, which is the superset.

    Nothing is reinterpreted: `steps: "ollama"` means one provider and no model, and
    `permission: "auto"` means that mode with nobody named to decide. The file simply
    carries the form that can show the fields beside them, so opening it names every
    option rather than half of one.
    """
    out, changed = dict(cfg), False
    for key, field in _SIDES.items():
        value = out.get(key)
        if not isinstance(value, str):
            continue
        if key == "permission":
            out[key] = {"mode": value or "ask", "provider": "", "model": ""}
        else:
            out[key] = {field: value, "model": ""}
        changed = True
    return out, changed

def _fill(have: dict, want: dict) -> tuple[dict, bool]:
    """`have` with every key `want` names that it is missing, and whether that added
    anything. Nested objects fill the same way, so a provider entry gains its blanks
    too. A key that is there keeps its value whatever it is: a config somebody
    hand-edited says what they meant, and a default must never overwrite it."""
    out, added = dict(have), False
    for key, value in want.items():
        if key not in out:
            out[key] = value
            added = True
        elif isinstance(value, dict) and isinstance(out[key], dict) and value:
            out[key], more = _fill(out[key], value)
            added = added or more
    return out, added

def load() -> dict:
    """A missing or unparseable config seeds itself on disk rather than living
    only in memory, so the file is there to hand-edit from first boot. One that
    exists gains any option it has not heard of and the long spelling of the three
    settings that take two, and is written back when that changed anything: the file
    a person opens is the whole template.

    Cached on `(mtime, size)`, because this is asked tens of times a turn -- once
    per entry lookup, per window, per skill pass -- and every one of them was a
    read and a parse of the same file. A hand-edit is still picked up on the next
    stat, which is the guarantee the callers already relied on ("config is re-read
    every turn: the user edits it by hand"); `save` empties the cache so our own
    writes never wait on a filesystem timestamp's resolution.

    The cached dict is never handed out: this returned the parse of a fresh file
    every call before, so a caller that mutated what it got lost that mutation on
    the next read. Returning the cache itself would make the same mutation stick in
    the memory of the whole process, and `entry()` hands out a sub-dict of it.
    """
    p = path()
    try: stamp = (str(p), p.stat().st_mtime_ns, p.stat().st_size)
    except OSError: stamp = None
    if stamp is not None and _cache.get("stamp") == stamp: return copy.deepcopy(_cache["cfg"])
    cfg = store.read_json(p)
    if not cfg:
        full = save(blank())
    else:
        full, added = _fill({**DEFAULTS, **cfg}, blank())
        full, respelled = _spelled(full)
        full = save(full) if added or respelled else full
    # Stamped with the value taken *before* the read, never a second stat: another
    # thread saving in between would otherwise have its new stamp attached to the
    # content we just read, and the cache would serve the old config until the file
    # was touched again. Under the old stamp the next caller re-reads, which is the
    # right answer either way.
    if stamp is not None: _cache.update(stamp=stamp, cfg=full)
    return copy.deepcopy(full)

def save(cfg: dict) -> dict:
    _cache.clear()
    store.write_json(path(), cfg)
    # The file holds API keys in plaintext, so 0600 is reapplied on every write
    # rather than trusted to stick.
    os.chmod(path(), 0o600)
    return cfg

def entry(name: str, cfg: dict = None) -> dict:
    return ((cfg or load()).get("providers") or {}).get(name) or {}

def settings(cfg: dict = None) -> dict:
    """The settings dict generate() takes. Model is omitted when unset so the
    provider's own default applies."""
    cfg = cfg or load()
    name = cfg.get("active") or "ollama"
    e = entry(name, cfg)
    out = {"provider": name}
    if e.get("model"): out["model"] = e["model"]
    # One endpoint can be pointed elsewhere per provider entry. A scheduled job
    # uses this to give its agent its own local server without a new registry key.
    if e.get("base_url"): out["base_url"] = str(e["base_url"])
    # Always stated, never inferred downstream: this is where the written flag
    # and what the model has already refused are reconciled.
    out["vision"] = can_see(name, cfg)
    if e.get("effort_map"): out["effort_map"] = e["effort_map"]
    params = params_for(name, out.get("model") or model_for(name, cfg), cfg)
    if params: out["params"] = params
    return out

def default_effort(cfg: dict = None) -> str:
    """The rung a new conversation starts on. Blank means the provider's own
    default, which is what an account that never set one gets."""
    level = ((cfg or load()).get("effort") or "").strip()
    return level if level in effort.LADDER else ""

def set_default_effort(level: str) -> dict:
    """Remember the rung, so the next session opens on it. Written whenever a level is
    chosen: a default that has to be set twice is a default nobody set."""
    level = (level or "").strip()
    if level and level not in effort.LADDER:
        raise ValueError(f"unknown effort: {level!r} (have {list(effort.LADDER)})")
    cfg = load()
    if cfg.get("effort") == level: return cfg
    cfg["effort"] = level
    return save(cfg)


def set_compact(value) -> dict:
    """Where the conversation folds: None for the automatic ratio, a number over 1
    for a token count, under 1 for a share of the window, 0 to never fold.

    One key for all three on purpose. A token limit is the thing a person actually
    knows ("keep it under 200k"); a share is the thing that survives a model swap.
    Storing whichever was said and reading it by size means /compress takes both
    without a second setting to keep in step with the first.

    This is the default for every model. A model with a different window can name
    its own count under `model_params[<model>].compact`, which wins for that model
    and leaves the rest alone.
    """
    cfg = load()
    if value is None: cfg.pop("compact", None)
    else: cfg["compact"] = value
    return save(cfg)


def permission_mode(cfg: dict = None) -> str:
    """Which of ask | auto | bypass | off governs tool permission prompts. ask (the default) puts each
    call to the user; auto hands them to a small model; bypass asks nothing either but keeps the
    irreversible calls and puts those to the user; off never asks. Unknown values land on ask.
    """
    p = (cfg or load()).get("permission")
    if isinstance(p, dict):
        mode = (p.get("mode") or "ask").strip().lower()
    else:
        mode = (p or "ask").strip().lower()
    return mode if mode in MODES else "ask"

def permission_decider(cfg: dict = None) -> tuple:
    """(provider, model) named to decide tool calls in auto mode. ("", "") inherits the active
    provider's own model.
        "permission": {"mode": "auto", "provider": "ollama", "model": "gemma4:31b"}
    """
    p = (cfg or load()).get("permission")
    if not isinstance(p, dict): return "", ""
    return (p.get("provider") or "").strip(), (p.get("model") or "").strip()

def api_key(name: str, cfg: dict = None) -> str:
    """Environment wins over the file, so a shell can override a stored key without editing anything.
    Last is whatever the provider finds on this machine: ollama is signed into through its desktop
    app, which leaves a usable key in ~/.ollama, so a working account can be configured nowhere at all.
    """
    p = providers.get(name)
    key_env = getattr(p, "key_env", "")
    return ((os.environ.get(key_env) if key_env else "") or entry(name, cfg).get("api_key")
            or getattr(p, "stored_key", lambda: "")() or "")

# Substrings that mean "this model will not take a picture", not any of the other things
# a 400 can mean. Matched loosely: every endpoint words it differently, none use a code.
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
    """Remember that this model cannot see. The `vision` flag is per provider, but vision is a property
    of the model: one entry points at a vision model today and a text-only one tomorrow. Learned like
    a rejected parameter, keyed by model, so a sighted one is not blocked by the last.
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

# Params that describe how this pairing is *described*, not what is sent. Read
# by name where needed, stripped from everything bound for the wire.
LOCAL_KEYS = frozenset({"context_window", "compact"})

def local_param(name: str, model: str, key: str, cfg: dict = None):
    """A LOCAL_KEY for this provider/model pair, per-model over provider-wide."""
    e = entry(name, cfg)
    for src in ((e.get("model_params") or {}).get(model) or {}, e.get("params") or {}, e):
        if (v := src.get(key)) is not None: return v
    return None

def params_for(name: str, model: str, cfg: dict = None) -> dict:
    """The native params this provider/model pair should be sent. `params` is what the endpoint always wants,
    `model_params[<model>]` what only that model wants, merged over it, so per-model wins. LOCAL_KEYS are dropped
    because they describe the pairing, and the openai dialect forwards params verbatim, where an unknown key 400s.
    """
    e = entry(name, cfg)
    merged = {**(e.get("params") or {}), **((e.get("model_params") or {}).get(model) or {})}
    return {k: v for k, v in merged.items() if k not in LOCAL_KEYS}


# Bumped whenever capabilities() learns to report something new: an older recorded answer
# is short, not wrong, and reading "absent" as "refused" once lost a thinking knob.
CAPS_REV = 2

def model_caps(name: str, model: str, cfg: dict = None) -> list | None:
    """What a provider said this model can do, if it was ever asked by a version of this
    code that looked for everything we read now."""
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
# keeps a second per-lifetime call from happening before it gets there.
_asked = set()

def _ask_caps(name: str, model: str, cfg: dict = None) -> list | None:
    """Ask the provider what the model can do, once, if it can answer at all. Only ollama can; everywhere
    else the answer comes from a rejected image, which is what learn_blind records. Failures are swallowed
    and nothing is written: a daemon that is down must leave the question open rather than answer it wrongly.
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
    """Whether this provider is expected to accept images. Four answers, in the order they deserve to be
    believed: what the user wrote, what the provider says about this exact model, what the model has refused,
    and the registry default. When another model is asked, the written flag is skipped: it describes the entry.
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
    """Whether this model has a thinking knob at all, or None if nobody knows. Three states, and the third
    matters: the effort table only defers to this when it is a definite no, so an unasked provider leaves
    the table's own guess standing.
    """
    model = model or model_for(name, cfg)
    caps = model_caps(name, model, cfg)
    if caps is None: caps = _ask_caps(name, model, cfg)
    return None if caps is None else "thinking" in caps

def effort_block(level: str, name: str = None, model: str = None, cfg: dict = None) -> str:
    """Why this rung cannot be set on the model in use, or "" if it can. Only "off" ever answers with a
    reason: accepting it and sending the lowest rung the model has would make the one setting that
    promises "do not think" the one that silently does not keep its promise.
    """
    cfg = cfg or load()
    name = name or cfg.get("active") or "ollama"
    model = model or model_for(name, cfg)
    p = providers.get(name)
    if effort.reachable(name, p.name, model, level, thinks=can_think(name, model, cfg),
                        override=entry(name, cfg).get("effort_map")):
        return ""
    return (f"{model or name} cannot stop thinking, so \"off\" would quietly mean its "
            f"lowest rung - pick that, or override effort_map[\"off\"] for {name}")

# Local-server probes, cached for the life of the process: this is read every
# turn to build the environment block.
_probed = {}

def _reachable(name: str, cfg: dict = None) -> bool:
    """Whether a request to this provider could get anywhere. A key settles it, and for ollama that includes the
    one its desktop app leaves behind. Otherwise only local providers are left, where "installed" is not "running":
    a base_url with nothing listening would be offered as the thing that can see, while every call fails to connect.
    """
    if api_key(name, cfg): return True
    url = getattr(providers.get(name), "base_url", "") or ""
    local = "localhost" in url or "127.0.0.1" in url
    if not local: return False
    if name not in _probed:
        _probed[name] = providers._daemon_up(url or None)
    return _probed[name]

def vision_helper(cfg: dict = None) -> tuple:
    """(provider, model) named to do the looking, or ("", "") for auto. Two spellings, a bare provider name or
    {"provider": ..., "model": ...}. The model override matters because the model that looks is often not the one
    holding the conversation; without it naming that provider is impossible, its configured model being blind.
    """
    v = (cfg or load()).get("vision")
    if isinstance(v, dict):
        return (v.get("provider") or "").strip(), (v.get("model") or "").strip()
    return (v or "").strip(), ""

def steps_helper(cfg: dict = None) -> tuple:
    """(provider, model) named to label the steps of a run, or ("", "") for nobody.

    The same two spellings `vision` takes, for the same reason: the model that
    names a step is rarely the model holding the conversation, and pointing this
    at a small one is the whole point. Unset falls back to `vision`, which is the
    other model this file already names for work that is not the conversation.
    """
    cfg = cfg or load()
    v = cfg.get("steps")
    if isinstance(v, dict):
        return (v.get("provider") or "").strip(), (v.get("model") or "").strip()
    if (v or "").strip():
        return v.strip(), ""
    # Not named: the model already set aside for the side jobs does this one, so
    # there is one place to point at a small model rather than two.
    return vision_helper(cfg)

def set_vision_helper(name: str, model: str = None) -> dict:
    """Name the provider that looks at images, or "" to go back to auto. Checked here rather than at use
    time: a helper that cannot see or cannot be reached would otherwise fail hours later, inside a tool
    call, and refusing it while someone is looking at the setting is the same information at a useful moment.
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
    """Every (provider, model) that could do the looking, best first. A pair rather than a name, because
    the named helper may carry a model override and the rest use whatever they are configured with. An
    empty model means "the one this provider already uses".
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
    """A provider that can see and could be reached, or "". A named helper wins while it still holds up: a key
    removed or a provider switched to vision:false must not leave the setting pointing at something that will
    fail. Otherwise the active provider is tried first, so a sighted setup never farms the work out.
    """
    found = lookers(cfg)
    return found[0][0] if found else ""

def use(name: str) -> dict:
    providers.get(name)          # raises on unknown, before anything is written
    cfg = load()
    cfg["active"] = name
    _WINDOWS.clear()             # the next turn asks the new provider its own size
    return save(cfg)

# Windows already established this process, keyed by provider and model. The endpoint is
# asked once per request, and the table clears on any switch so a new pairing is asked.
_WINDOWS: dict[tuple, int] = {}

def context_window(name: str, model: str, cfg: dict = None) -> int:
    """How many tokens this model's window holds, or 0 if nobody knows. Asked, never guessed: config.json,
    then the num_ctx the request sets, then the provider's own answer. 0 is a real answer, not a failure.
    Write context_window into the provider's `model_params[<model>]` to supply what the API will not.
    """
    # Resolved here rather than trusted from the caller: settings() omits the
    # model when it is unset, and a per-model window keyed by "" matches nothing.
    model = model or model_for(name, cfg)
    # local_param first: per-model over entry-wide, so switching models does not report the
    # last one's window. Then params_for: a num_ctx for this model is the request's window.
    for n in (local_param(name, model, "context_window", cfg),
              params_for(name, model, cfg).get("num_ctx")):
        if isinstance(n, int) and n > 0: return n

    key = (name, model)
    if key in _WINDOWS: return _WINDOWS[key]
    # Cached either way, including the zero: this runs on the path that reports a round's
    # token cost, and a slow provider must not be asked again every turn.
    try:
        p = providers.get(name)
        p.setup(api_key(name, cfg))
        found = p.context_window(model)
    except Exception as e:
        # Not cached, and said: caching a failure would hide the window for the
        # rest of the process, and a provider that answers later deserves to.
        faults.note(f"the context window of {name}/{model}", e,
                    "/context reports no window until a later turn asks again")
        return 0
    _WINDOWS[key] = found if isinstance(found, int) and found > 0 else 0
    return _WINDOWS[key]

def quirks(name: str, model: str, cfg: dict = None) -> list[str]:
    """Request keys this provider/model pair has already had rejected once. Keyed by model: one registry entry
    gets pointed at a reasoning model today and a plain one tomorrow, and a rejection by the latter must not
    permanently mute the former.
    """
    return list(((entry(name, cfg).get("quirks") or {}).get(model) or []))

def learn_quirk(name: str, model: str, keys: list[str]) -> dict:
    """Record a rejection so no later turn sends that key to this model again. On disk, not in memory: an
    unrecognized model costs exactly one failed request ever, not one per boot.
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
    # Per-level remap, merged so fixing "high" does not wipe a "max" set last
    # week. Keyed by canonical level; the value is native params, verbatim.
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
    # Same merge, one model deep. A null override removes, the only way to let the
    # provider-wide value through again; emptied models drop out of the file.
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
    # Model, num_ctx and key all change what the window is or who would be asked
    # about it, so nothing learned about the old pairing survives.
    _WINDOWS.clear()
    cfg.setdefault("providers", {})[name] = e
    return save(cfg)

def listing(cfg: dict = None) -> list[dict]:
    """Everything a frontend needs to render a picker. Never the key itself:
    this crosses the IPC wire and ends up in logs."""
    cfg = cfg or load()
    active = cfg.get("active") or "ollama"
    out = []
    for name in sorted(providers.PROVIDERS):
        p, e = providers.get(name), entry(name, cfg)
        model = e.get("model") or p.default_model
        drop, override = quirks(name, model, cfg), e.get("effort_map")
        # Stored caps only: listing walks every provider, and a picker opening is
        # no reason to ask each of them a question over the network.
        caps = model_caps(name, model, cfg)
        thinks = None if caps is None else "thinking" in caps
        out.append({"name": name, "dialect": p.name, "active": name == active,
                    "model": model,
                    "base_url": getattr(p, "base_url", None),
                    "vision": e.get("vision", getattr(p, "vision", True)),
                    # One rung list for every provider, plus what each rung would send
                    # to this model: a slider whose effect is invisible is not trusted.
                    "effort_rungs": list(effort.LADDER),
                    "effort_map": override or {},
                    "effort_preview": {lvl: effort.resolve(name, p.name, model, lvl,
                                                           drop=drop, override=override, thinks=thinks)
                                       for lvl in effort.LADDER},
                    # Whether the bottom rung means what it says here: the one rung that
                    # can be a lie rather than an approximation, so it is told.
                    "effort_off": effort.reachable(name, p.name, model, "off",
                                                   thinks=thinks, override=override),
                    "params": e.get("params") or {},
                    # What this entry's own model is sent on top of `params`. The whole map,
                    # not just the active row: a picker that hides rows makes them look unset.
                    "model_params": e.get("model_params") or {},
                    "has_key": bool(api_key(name, cfg))})
    return out
