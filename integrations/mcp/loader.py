"""Which MCP servers exist on this machine.

Same two places as subagents, workflows and skills: the ones here ship with the
app, the ones in ~/.cuacode are yours, and a name collision goes to yours. Read
every time rather than cached, so a server registered mid-conversation is
usable in the next turn.

The bundled file is deliberately empty. An MCP server is a local process with
the user's privileges, and often -- as with Spotify -- one that only makes
sense on one operating system or one person's machine. Nothing should be
running by default because it happened to be in the repo.

The config key is `mcpServers`, spelled the way Claude Desktop and Claude Code
spell it, so a block can be pasted between them without translation:

    {"mcpServers": {
        "spotify": {
            "command": "python3",
            "args": ["/path/to/server.py"],
            "description": "one line, shown to the model",
            "platform": "darwin"
        }
    }}
"""
from __future__ import annotations

import json
import platform
import sys
from pathlib import Path

from handler.session import store

BUNDLED = Path(__file__).parent / "servers.json"
FILENAME = "servers.json"


def user_dir() -> Path:
    d = store.home() / "mcp"
    d.mkdir(parents=True, exist_ok=True)
    return d


def user_config() -> Path:
    return user_dir() / FILENAME


def _read(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text() or "{}")
    except (json.JSONDecodeError, OSError) as exc:
        print(f"[mcp] ignoring {path}: {exc}", file=sys.stderr)
        return {}
    if not isinstance(data, dict):
        return {}
    # Bare {name: {...}} is accepted too, so a hand-written file that skipped
    # the wrapper still loads.
    servers = data.get("mcpServers", data)
    return {k: v for k, v in servers.items() if isinstance(v, dict)}


def load_servers(include_disabled: bool = False) -> dict[str, dict]:
    found: dict[str, dict] = {}
    for path in (BUNDLED, user_config()):                       # user second: it overwrites
        found.update(_read(path))

    out = {}
    for name, cfg in found.items():
        if not include_disabled:
            if cfg.get("enabled") is False:
                continue
            # A macOS-only server on Linux is not an error to report later, it
            # is a server that should never have been offered.
            wanted = cfg.get("platform")
            if wanted and platform.system().lower() != str(wanted).lower():
                continue
        out[name] = cfg
    return dict(sorted(out.items()))


def get(name: str) -> dict:
    servers = load_servers()
    if name not in servers:
        known = ", ".join(servers) or "none registered"
        raise ValueError(f"unknown MCP server: {name!r} (have {known})")
    return servers[name]
