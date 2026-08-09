---
name: mcp
output:
  servers: list
  tools: list
  data: object
  text: string
active: True
require_permissions: True
backgroundable: False
---
Reaches the MCP (Model Context Protocol) servers registered on this machine.
An MCP server is a separate program that exposes its own tools — the Spotify
app, a database, a company's internal API — and this tool is how you call them.

Work in two steps. `load` starts one server and returns the tools it offers
with their argument schemas; `call` then runs one of those tools. Load a server
before calling it: the schemas are not in your context until you ask, which is
why a list of servers costs a line each instead of a document each. A server
stays running once loaded, so load it once per conversation, not once per call.

- `list` — every registered server, whether it is loaded, and the file they are
  registered in.
- `load` — start `server` and return its tools, their descriptions and their
  input schemas.
- `call` — run `tool` on `server` with `arguments` matching the schema `load`
  reported. Pass the arguments exactly as named there.
- `stop` — shut a server down. Rarely needed; they close when the app exits.

A tool that fails comes back as an error carrying the server's own explanation
— "Spotify is not running" is something to act on, not a bug to retry blindly.

MCP servers are local processes that run with the user's privileges, and they
are registered by hand in ~/.cuacode/mcp/servers.json. Nothing ships enabled.
Do not invent a server name; only the ones `list` reports exist.
