import json, os, yaml, importlib.util
from pathlib import Path
from dataclasses import dataclass
from typing import Callable

from tools._parser.Validate import check
from handler import faults

@dataclass
class Tool:
    name: str
    description: str
    input_schema: dict
    output_schema: dict
    active: bool
    handler: Callable
    require_permissions: bool
    # Set only for tools that answer describe()/schema() at runtime, so they can
    # be re-asked without re-executing every main.py. body is the Description.md
    # text describe() was handed.
    module: object = None
    body: str = ""
    # Offered only to a model that cannot see, since standing in for eyes is its
    # whole job.
    blind_only: bool = False
    # Withheld from a model that cannot see. A coordinate is read off a
    # screenshot; with none, the call is a guess that lands somewhere real and
    # cannot be taken back.
    needs_sight: bool = False
    # May be started and left to finish on its own. Declared per tool because it
    # is only true of the slow ones that return an answer (a build, a fetch, a
    # subagent), and meaningless on a click, whose value is having happened by
    # the next call.
    backgroundable: bool = False
    # What this call would do, asked before the user is asked to allow it. Only
    # defined by a tool that can say something the arguments do not (file draws
    # the diff an edit would produce); the loop shows the arguments alone for
    # every tool that does not.
    preview: Callable = None
    # Per-call danger, asked of the tool itself: require_permissions is
    # tool-wide, but most tools are only sometimes dangerous (a file read, a
    # harmless command). safe() says which; without it every call is asked.
    safe: Callable = None
    # What a frontend's row says about one result, when the result itself is too
    # big to be the row. Declared by the tool, because only it knows which of its
    # keys is a payload -- `stdout` for a shell, `image_base64` for a screenshot,
    # `output` for a fetch. A tool with none sends its result whole, which is what
    # the small ones want.
    #
    # Three hardcoded name lists in main.py used to decide this from outside the
    # tools, so a new image-returning tool shipped its base64 to every frontend
    # until somebody remembered the list.
    brief: Callable = None
    # Why this call is one there is nothing to undo after, or "". Asked only by
    # bypass mode, which waves everything else through: it is the tool that knows,
    # because it is the tool that decides what "deleted" means (the file tool puts
    # what it removes in the Trash; shell can wipe a disk).
    irreversible: Callable = None

def brief_body(*keys: str):
    """A `brief` for a tool whose result carries a document under one key.

    The body is already in `messages`, where the model reads it, so the row says
    how big it was instead and keeps every small fact around it -- a truncated
    fetch, a failed digest, a step count. A count is what a person wants from a
    body anyway: "the page came back" and "the page came back with 90k
    characters" are different rows.

    keys default to the five the wire has carried bodies under so far; a tool with
    a differently-named body passes its own.
    """
    drop = keys or ("output", "log", "text", "instructions", "description")

    def brief(result: dict) -> dict:
        r = result.get("result")
        if not isinstance(r, dict): return result
        row = {k: v for k, v in r.items() if k not in drop}
        # Measured off whichever key carried it: one number says the same thing about
        # a page, a skill body and a transcript. Reading only `output` and
        # `instructions` meant a tool that named its body something else had the body
        # dropped from the row with nothing in its place.
        body = next((r[k] for k in drop if isinstance(r.get(k), str)), None)
        if isinstance(body, str): row["chars"] = len(body)
        elif isinstance(body, dict): row["fields"] = sorted(body)[:8]
        if isinstance(r.get("log"), list): row["steps"] = len(r["log"])
        return {"result": row}
    return brief

def parse_frontmatter(text: str) -> tuple[dict, str]:
    if not text.startswith("---"): return {}, text.strip()
    _, meta_block, body = text.split("---", 2)
    return yaml.safe_load(meta_block) or {}, body.strip()

def _load_main(path: Path):
    spec = importlib.util.spec_from_file_location(f"tools.{path.parent.name}.main", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod

def load_tools(tools_dir="tools") -> dict[str, Tool]:
    registry = {}
    for folder in sorted(Path(tools_dir).iterdir()):
        if not folder.is_dir() or folder.name.startswith("_"): continue
        desc_f, schema_f, main_f = folder / "Description.md", folder / "InputSchema.json", folder / "main.py"
        missing = [f.name for f in (desc_f, schema_f, main_f) if not f.exists()]
        if missing: raise RuntimeError(f"{folder.name}: missing {missing}")
        meta, body = parse_frontmatter(desc_f.read_text(encoding="utf-8"))
        mod = _load_main(main_f)
        if not hasattr(mod, "run"): raise RuntimeError(f"{folder.name}: main.py has no run()")
        # Options that only exist at runtime (installed subagents, workflows)
        # cannot be written into a file months earlier. describe() and schema()
        # let main.py answer instead; a tool with neither loads as before.
        registry[folder.name] = Tool(
            name=meta.get("name", folder.name),
            description=mod.describe(body) if hasattr(mod, "describe") else body,
            input_schema=mod.schema() if hasattr(mod, "schema") else json.loads(schema_f.read_text(encoding="utf-8")),
            output_schema=meta.get("output", {}),
            active=meta.get("active", True),
            require_permissions=meta.get("require_permissions", False),
            blind_only=meta.get("blind_only", False),
            needs_sight=meta.get("needs_sight", False),
            backgroundable=meta.get("backgroundable", False),
            handler=mod.run,
            preview=getattr(mod, "preview", None),
            safe=getattr(mod, "safe", None),
            brief=getattr(mod, "brief", None),
            irreversible=getattr(mod, "irreversible", None),
            module=mod if (hasattr(mod, "describe") or hasattr(mod, "schema")) else None,
            body=body,
        )
    return registry

def refresh_dynamic(registry: dict[str, Tool]) -> dict[str, Tool]:
    """Re-ask the self-describing tools what they offer.

    The registry is built once per process, and an agent that writes a new
    subagent file has to run it in the same conversation. Only the tools that
    answer at runtime are asked again, so no other main.py is re-executed. A
    hook that raises leaves the previous value in place.
    """
    for t in registry.values():
        if t.module is None: continue
        try:
            if hasattr(t.module, "describe"): t.description = t.module.describe(t.body)
            if hasattr(t.module, "schema"): t.input_schema = t.module.schema()
        except Exception as e:
            # Keeping the previous value is right -- a menu that cannot be rebuilt
            # is better than no menu -- but a tool whose list is stale for the life
            # of the process is not the same as one whose list did not change.
            faults.note(f"re-asking {t.name} what it offers", e,
                        "it keeps the description and schema it had")
    return registry

# The registry, and the question of which of it a given model may be offered.
#
# It lives here rather than in the agent loop because two readers ask it: the loop,
# which sends the tools, and handler/context.py, which prices them for /context. A
# readout depending on the loop to ask what the toolbox holds is a stable module
# importing a volatile one, and the direction of that import was the only reason it
# had to.
_registry = None

def registry(tools_dir=None) -> dict:
    """Every installed tool, loaded once per process.

    Loaded once, since load_tools re-execs every tool's main.py. Refreshed each
    turn for the self-describing tools only, so an agent can use a subagent or
    skill file it just wrote. Unchanged entries come back byte-identical, so the
    prompt cache survives.

    `CUACODE_TOOLS` names another directory. A scheduled job sets it to its
    per-run layer of built-ins plus its own tools, so one job can add or replace a
    tool without every other conversation seeing it. Unset means this tree.
    """
    global _registry
    if _registry is None:
        base = tools_dir or os.environ.get("CUACODE_TOOLS") or Path(__file__).parent
        _registry = load_tools(tools_dir=base)
    return refresh_dynamic(_registry)

def needs_vision(t) -> bool:
    """Read off the tool's declared output, not a name list, so a new
    image-returning tool is covered the day it lands."""
    return any("base64" in str(v) for v in (t.output_schema or {}).values())

def visible(reg: dict, vision: bool) -> dict:
    """The toolbox this model can actually use. Offered a camera it will call it,
    and a rejected image costs the whole turn."""
    if vision: return {k: t for k, t in reg.items() if not t.blind_only}
    # Withheld for two different reasons: an image-returning tool fails the request
    # outright, a coordinate-taking one silently succeeds somewhere the model never
    # looked, which is worse.
    return {k: t for k, t in reg.items() if not needs_vision(t) and not t.needs_sight}

def wire_result(registry: dict, name: str, result: dict) -> dict:
    """The copy of one result that crosses to a frontend.

    One place, so a live turn and a reopened conversation send the same thing: a
    replay used to ship the stored result whole while the live turn sent the
    brief, so reopening a session redrew a build log that the turn itself had
    reduced to a byte count. A brief that raises falls back to the whole result:
    a row is not worth losing to a formatter bug.
    """
    fn = getattr(registry.get(name), "brief", None)
    if not fn: return result
    try: return fn(result)
    except Exception as e:
        # The whole result, which is what the next-best row is; said, because the
        # row that arrives is a megabyte of base64 and the reason is a formatter.
        faults.note(f"the brief for {name}", e, "the whole result is sent instead")
        return result

def dispatch(registry: dict[str, Tool], name: str, args: dict, ctx=None) -> dict:
    tool = registry.get(name)
    if not tool: return {"error": f"unknown tool: {name}"}
    # Checked against the schema before the handler, so a missing field comes
    # back as a sentence instead of a KeyError. Defaults stay unfilled: the
    # handler's own args.get() owns what absent means.
    args, errs = check(args or {}, tool.input_schema)
    if errs: return {"error": "; ".join(errs)}
    try: return {"result": tool.handler(args, ctx)}
    except Exception as e:
        # Named, because the model reads this and knows which call it made; the
        # exception alone could have come from any tool in the round.
        return {"error": f"{name}: {e}"}
