import json, yaml, importlib.util
from pathlib import Path
from dataclasses import dataclass
from typing import Callable

from tools._parser.Validate import check

@dataclass
class Tool:
    name: str
    description: str
    input_schema: dict
    output_schema: dict
    active: bool
    handler: Callable
    require_permissions: bool
    # Kept only for the tools that describe themselves at runtime, so their
    # description and schema can be rebuilt without re-executing every main.py
    # in the tree. None for a tool whose files already say everything; body is
    # the Description.md text describe() was originally handed.
    module: object = None
    body: str = ""
    # The mirror of an image-returning tool: offered only to a model that
    # cannot see, because its whole job is standing in for eyes.
    blind_only: bool = False
    # Withheld from a model that cannot see, without returning an image itself.
    # A coordinate is read off a screenshot; with no screenshot the call is a
    # guess that lands somewhere real and cannot be taken back.
    needs_sight: bool = False
    # May be started and left to finish on its own. Declared per tool because
    # it is only ever true of the slow ones that return an answer -- a build, a
    # fetch, a subagent. It is meaningless on a click, whose whole value is
    # having happened by the time the next call is decided, and offering the
    # flag there would only invite the model to fire one into the dark.
    backgroundable: bool = False
    # What this call would do, asked for before the user is asked to allow it.
    # Only a tool that can say something the arguments do not already say defines
    # it -- file draws the diff an edit would produce -- and the loop shows the
    # arguments alone for every tool that does not.
    preview: Callable = None
    # Per-call danger, asked of the tool itself: require_permissions is
    # tool-wide, but most tools are only sometimes dangerous (a file read, a
    # harmless command). A tool with safe() says which; one without is asked
    # every time.
    safe: Callable = None

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
        meta, body = parse_frontmatter(desc_f.read_text())
        mod = _load_main(main_f)
        if not hasattr(mod, "run"): raise RuntimeError(f"{folder.name}: main.py has no run()")
        # A tool whose options only exist at runtime -- which subagents are
        # installed, which workflows -- cannot say so in a file written months
        # earlier. describe() and schema() let main.py answer instead, and a
        # tool that defines neither is loaded exactly as before.
        registry[folder.name] = Tool(
            name=meta.get("name", folder.name),
            description=mod.describe(body) if hasattr(mod, "describe") else body,
            input_schema=mod.schema() if hasattr(mod, "schema") else json.loads(schema_f.read_text()),
            output_schema=meta.get("output", {}),
            active=meta.get("active", True),
            require_permissions=meta.get("require_permissions", False),
            blind_only=meta.get("blind_only", False),
            needs_sight=meta.get("needs_sight", False),
            backgroundable=meta.get("backgroundable", False),
            handler=mod.run,
            preview=getattr(mod, "preview", None),
            safe=getattr(mod, "safe", None),
            module=mod if (hasattr(mod, "describe") or hasattr(mod, "schema")) else None,
            body=body,
        )
    return registry

def refresh_dynamic(registry: dict[str, Tool]) -> dict[str, Tool]:
    """Re-ask the self-describing tools what they offer.

    An agent that writes a new subagent file has to be able to run it in the
    same conversation, and the registry is built once per process. Rather than
    reloading every tool -- which re-executes every main.py -- only the tools
    that answer this question at runtime are asked again, and only they pay for
    it. A tool whose hook raises keeps whatever it last had.
    """
    for t in registry.values():
        if t.module is None: continue
        try:
            if hasattr(t.module, "describe"): t.description = t.module.describe(t.body)
            if hasattr(t.module, "schema"): t.input_schema = t.module.schema()
        except Exception: pass
    return registry

def dispatch(registry: dict[str, Tool], name: str, args: dict, ctx=None) -> dict:
    tool = registry.get(name)
    if not tool: return {"error": f"unknown tool: {name}"}
    # Schema-checked before the handler: a missing field becomes a sentence the
    # model can act on instead of a KeyError as a result. Defaults stay unfilled
    # -- the handler's own args.get() owns what absent means.
    args, errs = check(args or {}, tool.input_schema)
    if errs: return {"error": "; ".join(errs)}
    try: return {"result": tool.handler(args, ctx)}
    except Exception as e: return {"error": str(e)}
