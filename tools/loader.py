import json, yaml, importlib.util
from pathlib import Path
from dataclasses import dataclass
from typing import Callable

@dataclass
class Tool:
    name: str
    description: str
    input_schema: dict
    output_schema: dict
    active: bool
    handler: Callable
    require_permissions: bool

def _parse_frontmatter(text: str) -> tuple[dict, str]:
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
        meta, body = _parse_frontmatter(desc_f.read_text())
        mod = _load_main(main_f)
        if not hasattr(mod, "run"): raise RuntimeError(f"{folder.name}: main.py has no run()")
        registry[folder.name] = Tool(
            name=meta.get("name", folder.name),
            description=body,
            input_schema=json.loads(schema_f.read_text()),
            output_schema=meta.get("output", {}),
            active=meta.get("active", True),
            require_permissions=meta.get("require_permissions", False),
            handler=mod.run,
        )
    return registry

def dispatch(registry: dict[str, Tool], name: str, args: dict, ctx=None) -> dict:
    tool = registry.get(name)
    if not tool: return {"error": f"unknown tool: {name}"}
    try: return {"result": tool.handler(args, ctx)}
    except Exception as e: return {"error": str(e)}
