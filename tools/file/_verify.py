import ast, json
from pathlib import Path

try: import yaml
except ImportError: yaml = None
try: import tomllib
except ImportError: tomllib = None

def check(path: Path, text: str) -> str | None:
    """A syntax error caught now instead of the next time something runs the file.

    Cheap because it is only ever a parse of text already in memory, and worth it
    because the alternative is the agent discovering a stray bracket several tool
    calls later, with no idea which of its edits left it there.

    The write still stands: rolling back would leave the agent editing against
    content that is no longer on disk, and it needs the broken file to fix it.
    """
    suf = path.suffix.lower()
    try:
        if suf in (".py", ".pyi"): ast.parse(text, filename=str(path))
        elif suf == ".json": json.loads(text)
        elif suf in (".yaml", ".yml") and yaml: yaml.safe_load(text)
        elif suf == ".toml" and tomllib: tomllib.loads(text)
        else: return None
    except SyntaxError as e:
        return f"line {e.lineno}: {e.msg}"
    except Exception as e:
        return f"{type(e).__name__}: {e}"
    return None
