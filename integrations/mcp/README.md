# MCP servers

An MCP server is a separate program that exposes tools over the Model Context
Protocol. CuaCode speaks to them as a client: it starts the server, asks what
it offers, and calls it. That is how the agent reaches something this codebase
does not implement — a desktop app, a database, an internal API — without a
tool folder being written for it.

Two pieces live here. `client.py` is the protocol: JSON-RPC 2.0 over the
server's stdin and stdout, or over HTTP, stdlib only, no new dependency.
`loader.py` is the registry — which servers exist on this machine. The agent's
side is the `mcp` tool in `tools/mcp/`.

    integrations/mcp/servers.json     ships with the app — empty
    ~/.cuacode/mcp/servers.json       yours; wins on a name collision

The bundled file is empty and should stay that way unless a server is genuinely
cross-platform and genuinely belongs to everyone. An MCP server is a local
process running with your privileges, and a lot of them — the Spotify one below
included — only make sense on one OS or one person's machine. Nothing runs by
default because it happened to be committed.

## Registering one

```json
{
  "mcpServers": {
    "spotify": {
      "command": "python3",
      "args": ["/Users/you/Documents/Work/CuaCode-core/integrations/mcp/servers/spotify/server.py"],
      "description": "One line. The agent reads this to decide whether to load the server.",
      "platform": "darwin"
    }
  }
}
```

The key is spelled `mcpServers`, the way Claude Desktop and Claude Code spell
it, so a block can be pasted between them unchanged. A bare `{"name": {...}}`
without the wrapper also loads.

| field | |
|---|---|
| `command` | The program. `python3` and `python` are replaced with the interpreter already running CuaCode, which is one that certainly exists. |
| `args` | Argument list. `~` is expanded. |
| `env` | Extra environment variables, merged over the inherited ones. |
| `cwd` | Working directory for the server process. |
| `description` | One line, and the only thing about this server that is in the model's context by default. Write it for the model. |
| `platform` | Optional. `darwin`, `linux`, `windows` — the server is not offered anywhere else. |
| `enabled` | `false` hides it without deleting the entry. |
| `transport` | `stdio` (default) or `http`. With `http` the process is still launched here, and `url` is where it listens. |
| `url` | The MCP endpoint, for `transport: "http"`. |
| `protocol` | `auto` (default), `modern`, `legacy`, or a revision date like `2026-07-28` — see below. |
| `session_tool` | The tool that mints a session handle. Default `session_new`. |
| `session_argument` | The argument that carries a handle back to a stateless server. Default `session`. |

The file is re-read every turn, so a server registered mid-conversation is
usable in the next one. No restart.

## Two eras

MCP split in two at revision `2026-07-28`, and this client speaks both.

**Legacy** — `2025-11-25` and earlier. The connection opens with an
`initialize` handshake, and the connection *is* a session: over HTTP the server
names it with an `Mcp-Session-Id` header and routes by it.

**Modern** — `2026-07-28`. Stateless. No handshake, no session header. Every
request carries its own protocol version, client identity and capabilities in
`_meta`, so any instance can serve any request. A server that needs state
across calls mints a handle and takes it back as an ordinary tool argument.
Over HTTP a few body fields are mirrored into headers — `Mcp-Method`,
`Mcp-Name`, `MCP-Protocol-Version`, and any parameter the tool's schema marks
with `x-mcp-header` — and the server rejects the request if they disagree with
the body. A modern server may answer any request with an SSE stream instead of
a JSON object; the client reads either.

Which one a server speaks is decided once, at startup, by the probe the spec
prescribes: send `server/discover`, and treat anything that is not a
`DiscoverResult` or a recognised modern error as a legacy server to open with
`initialize`. Nothing has to be declared in the config for this to work.

It costs something, though. A legacy server that answers unknown methods with
an error — most of them, including the Spotify server here — is identified
instantly. One that ignores them costs `PROBE_TIMEOUT` (3s) once per session
before the client gives up and falls back. `"protocol": "legacy"` skips the
probe for such a server; `"protocol": "modern"` refuses to fall back, which is
worth setting only to catch a stateless server that has silently regressed.

Two parts of the modern spec are not implemented, and both fail loudly rather
than quietly: multi round-trip requests (a result of `resultType:
"input_required"`, where the server wants sampling or elicitation mid-call)
come back as an error, and `subscriptions/listen` is never opened, so
list-changed notifications are not received. Neither affects calling tools.

### Sessions across the two

A subagent run gets its own session scope so parallel subagents do not share
one browser page. On a legacy HTTP server that scope holds an `Mcp-Session-Id`
and travels as a header, exactly as before. On a modern server there is no such
header: the client calls `session_tool` once per run, keeps the handle it
returns, and passes it as `session_argument` on any tool whose own input schema
declares that argument. A server with no such tool, or a tool that does not
take the argument, is called exactly as it would be otherwise — no handle is
invented.

## How the agent uses it

The `mcp` tool has four actions: `list`, `load`, `call`, `stop`. Only the
server names and their one-line descriptions are ever in context by default —
`load` is what spends context on one server's tool schemas, and `call` runs a
tool.

That indirection is the point. Folding every MCP tool into the model's own tool
list would put thirty argument schemas in front of it for the whole
conversation whether or not the subject ever comes up. Skills already solved
this here: carry the description, load the body on demand. Same trick.

A server process starts on first use and is kept for the rest of the session,
so the interpreter launch and handshake are paid once, not per call.

## The Spotify server

`servers/spotify/server.py` — controls the Spotify **desktop app** on macOS
through AppleScript. No OAuth, no developer account, no network. It is not
registered by default; add the block above to `~/.cuacode/mcp/servers.json`.

Spotify has to already be open. The server checks before every command and
returns "Spotify is not running" rather than launching the app, because a model
asking what is playing should not open a window nobody asked for.

Sixteen tools:

| | |
|---|---|
| `get_current_track` | name, artist, album, duration, position, a one-line summary |
| `get_playback_state` | all of it at once — state, position, volume, shuffle, repeat, track |
| `get_playback_position` | playhead, duration, time remaining |
| `play` `pause` `playpause` | transport |
| `next_track` `previous_track` | skip, and return what started playing |
| `set_volume` `volume_up` `volume_down` | Spotify's own volume, 0–100, clamped |
| `seek` | jump to a position, refused past the end of the track |
| `set_shuffle` `set_repeat` | omit `enabled` to toggle |
| `get_current_context` | see below |
| `play_uri` | play a `spotify:` URI, optionally inside a playlist or album |

Two things it cannot do, both the app's limits rather than this server's:

**No playlist.** Spotify's scripting dictionary has no playlist or context
property — the app publishes the track, not where the track came from.
`get_current_context` returns `available: false` with the reason in words, plus
the album and track URL, rather than handing back the album and letting it be
mistaken for the answer. Real playback context needs the Web API and a
developer account.

**Volume is quantized.** Spotify snaps its volume to its own steps; ask for 46
and it may read back 45. Every volume tool returns the value read back after
setting, not the value requested.

The desktop app's local HTTP API (port 4371 and the others) does not answer at
all here, which is why none of this depends on it.

### Running it by hand

Stdlib only, so any Python 3 works — no venv, no install.

```bash
python3 integrations/mcp/servers/spotify/server.py --selftest
```

That prints the tool list and calls `get_current_track` and
`get_playback_state` against the running app, without an MCP client in the way.
To exercise the actual protocol:

```bash
printf '%s\n' \
  '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"probe","version":"1"}}}' \
  '{"jsonrpc":"2.0","method":"notifications/initialized","params":{}}' \
  '{"jsonrpc":"2.0","id":2,"method":"tools/list","params":{}}' \
  '{"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"get_current_track","arguments":{}}}' \
| python3 integrations/mcp/servers/spotify/server.py
```

That is the legacy opening. A stateless server takes no handshake at all — the
version and capabilities ride on every request:

```bash
printf '%s\n' \
  '{"jsonrpc":"2.0","id":1,"method":"server/discover","params":{"_meta":{"io.modelcontextprotocol/protocolVersion":"2026-07-28","io.modelcontextprotocol/clientInfo":{"name":"probe","version":"1"},"io.modelcontextprotocol/clientCapabilities":{}}}}' \
  '{"jsonrpc":"2.0","id":2,"method":"tools/list","params":{"_meta":{"io.modelcontextprotocol/protocolVersion":"2026-07-28","io.modelcontextprotocol/clientCapabilities":{}}}}' \
| python3 /path/to/stateless_server.py
```

Sending the first of those to the Spotify server is exactly what this client
does at startup; the `-32601` that comes back is what tells it to open with
`initialize` instead.

### Other MCP clients

It is an ordinary stdio MCP server, so anything that speaks the protocol can
run it. Claude Desktop wants the same block in
`~/Library/Application Support/Claude/claude_desktop_config.json`; Claude Code
takes it with:

```bash
claude mcp add spotify -- python3 /path/to/integrations/mcp/servers/spotify/server.py
```

## Writing your own

A server is any program that reads JSON-RPC from stdin and writes it to stdout,
one message per line. Legacy needs `initialize`, `tools/list` and `tools/call`
and nothing else; `servers/spotify/server.py` implements them in about a
hundred lines and is a reasonable thing to copy. A stateless one drops
`initialize`, must answer `server/discover`, must put `"resultType":
"complete"` on every result, and should read the version and capabilities out
of each request's `_meta` rather than remembering them. Write whichever era you
like — the client probes and adapts either way.

One rule that is easy to get wrong: **stdout carries protocol and nothing
else.** A stray `print` is a parse error at the client. Send diagnostics to
stderr — CuaCode keeps the last few lines and quotes them when a server dies,
so that is where they are useful anyway.

Report a tool's own failures as a result with `isError: true`, not as a
JSON-RPC error. "Spotify is not running" is something the model can act on; a
protocol error is something it can only retry.
