---
name: adding-mcp-servers
description: Register or write an MCP server so CuaCode can reach tools this codebase does not implement — a desktop app, a database, a third-party server. Use when the user wants to add an MCP, asks why an MCP is not showing up, or asks what MCP servers they have.
---
An MCP server is a separate program exposing tools over the Model Context
Protocol. CuaCode is the client: it starts the server, asks what it offers, and
calls it. That is how you reach something this codebase has no tool for.

Two different jobs live here. **Registering** an existing server is a few lines
of JSON and needs no code. **Writing** one is only for a capability no server
provides yet. Most requests are the first; read that half and stop.

Reach for a tool in `tools/` instead when the capability belongs to CuaCode
itself and every user should have it. Reach for MCP when it is somebody else's
program, or one person's machine.

## Registering

One file, and the user's copy is the one that matters:

```
integrations/mcp/servers.json    ships with the app — empty, and stays empty
~/.cuacode/mcp/servers.json      theirs; wins on a name collision
```

```json
{
  "mcpServers": {
    "spotify": {
      "command": "python3",
      "args": ["/absolute/path/to/server.py"],
      "description": "One line. This is all you see until you load the server.",
      "platform": "darwin"
    }
  }
}
```

| field | |
|---|---|
| `command` | The program. `python3`/`python` become the interpreter already running CuaCode, which certainly exists — a bare `python3` on a Mac with several installs is a coin toss. |
| `args` | Argument list. `~` is expanded. Use **absolute paths**; the server's working directory is not the repo. |
| `env` | Extra environment variables, merged over inherited ones. Where an API key goes. |
| `cwd` | Working directory for the server process. |
| `description` | One line, written for the model. The only thing about this server in context by default, so it decides whether the server is ever loaded. |
| `platform` | `darwin`/`linux`/`windows`. The server is not offered anywhere else. |
| `enabled` | `false` hides it without deleting the entry. |

The file is re-read every turn. A server registered mid-conversation works in
the next one — never tell the user to restart.

**Do not add servers to the repo's `servers.json`.** It ships empty on purpose:
an MCP server is a local process with the user's privileges, and most are
OS-bound or personal. Something belongs there only if it works everywhere and
belongs to everyone. Everything else is a `~/.cuacode` registration with a
`platform` gate.

### Then check it

```bash
python3 -c "
from tools.loader import load_tools, dispatch
reg = load_tools('tools')
print(dispatch(reg, 'mcp', {'action': 'load', 'server': '<name>'}))"
```

Or just call the `mcp` tool: `list` shows what is registered and whether it is
up, `load` starts one and returns its tools.

When it fails, the error carries the server's own stderr — that is where the
reason is. Usual causes: a relative path in `args`, a missing dependency in the
interpreter that actually launched, or the server printing something to stdout.

## Writing one

`integrations/mcp/servers/spotify/server.py` is the worked example: stdlib only,
stdio, about a hundred lines. Copy its shape.

Three methods are the whole required surface. Messages are one JSON object per
line, in both directions.

```python
import json, sys

TOOLS = [{"name": "do_thing",
          "description": "What it does, written for the model.",
          "inputSchema": {"type": "object", "additionalProperties": False,
                          "properties": {"x": {"type": "string", "description": "..."}},
                          "required": ["x"]}}]

def send(msg):
    sys.stdout.write(json.dumps(msg) + "\n"); sys.stdout.flush()

for line in sys.stdin:
    if not line.strip(): continue
    msg = json.loads(line)
    mid, method = msg.get("id"), msg.get("method")
    if mid is None: continue                      # notification: nothing to answer
    if method == "initialize":
        send({"jsonrpc": "2.0", "id": mid, "result": {
            "protocolVersion": (msg.get("params") or {}).get("protocolVersion", "2025-06-18"),
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": "mine", "version": "1.0.0"}}})
    elif method == "tools/list":
        send({"jsonrpc": "2.0", "id": mid, "result": {"tools": TOOLS}})
    elif method == "tools/call":
        params = msg.get("params") or {}
        text = run_tool(params["name"], params.get("arguments") or {})
        send({"jsonrpc": "2.0", "id": mid, "result": {
            "content": [{"type": "text", "text": text}], "isError": False}})
    else:
        send({"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": "no such method"}})
```

Four rules worth more than the rest:

- **stdout carries protocol and nothing else.** One stray `print` is a parse
  error at the client. Diagnostics go to stderr, which CuaCode keeps and quotes
  when a server dies — so that is where they are useful anyway.
- **A tool's own failure is a result, not a JSON-RPC error.** Return
  `isError: true` with the reason in words. "Spotify is not running" is
  something the model can act on; a protocol error is something it can only
  retry. Reserve JSON-RPC errors for unknown methods and malformed requests.
- **Return JSON as the text block.** CuaCode parses it, so the model gets an
  object rather than a string containing one.
- **One bad request must not kill the server.** Catch around the dispatch; the
  client sees a dead pipe instead of a reason otherwise.

Add a `--selftest` branch that calls a couple of tools directly and prints the
result. It is how the user checks the server without a client in the way, and
how you check it without a round trip.

## What the agent sees

Only server names and their one-line descriptions, until `load` is called on
one. That is deliberate — the same bargain skills make. So the `description` in
the config, and each tool's own `description`, are doing real work: they are
the entire basis for deciding whether the server gets loaded at all.

`integrations/mcp/README.md` has the rest: the client, the pool, the protocol,
and the Spotify server's two limits.
