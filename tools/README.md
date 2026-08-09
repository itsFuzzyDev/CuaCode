Tools dir, builds all tools automatically, attches into provider. _parser folder handles turning each tool schmea into required schema for the AI providers

tools:
- [x] screenshot (grid_size)
- [x] mouse_move (x, y) 
- [x] click (x, y, button)
- [x] type_text (text)
- [x] key (key combo, e.g. "cmd+c")
- [x] scroll (x, y, dx, dy)
- [x] wait (seconds)
- [x] app_open (app_name)
- [x] app_list - includes closed and opened apps.
- [x] file (action: read/write/edit/glob/grep/ls/mkdir/move/delete/undo) - edits are all-or-nothing and return a diff, writes are atomic, and a file must be read before it can be overwritten or edited
- [x] shell (command, cwd, timeout) - login shell, cwd persists across calls
- [x] WebFetch (url, goal, mode) - fetched here, trafilatura only turns html into md. digest mode runs a subagent over the page and returns a validated dict instead of the page
- [ ] WebSearch (query) - stub, returns not-implemented
- [x] agent (agent, prompt) - runs one subagent from integrations/subagents or ~/.cuacode/subagents
- [x] workflow (workflow, args) - runs one script from integrations/workflows or ~/.cuacode/workflows
- [x] skill (skill) - loads one SKILL.md from integrations/skills or ~/.cuacode/skills
- [x] mcp (action: list/load/call/stop) - reaches MCP servers registered in ~/.cuacode/mcp/servers.json. load returns one server's tool schemas, call runs one; only server names are in context until then. See integrations/mcp/README.md

A tool whose options only exist at runtime can define describe(body) and/or
schema() in main.py; the loader calls them instead of reading Description.md's
body and InputSchema.json. That is how agent and workflow offer the model an
enum of what is actually installed on this machine. Tools that define neither
load exactly as before.

A tool may also define preview(args, ctx) in main.py. The round loop calls it
just before asking the user to allow the call, and sends what it returns --
{"summary": str, "diff": str} -- alongside the arguments in the permission
request, so the dialog can show the patch an edit would apply rather than the
strings it would apply it with. It must not change anything: nobody has said
yes yet. A preview that raises is dropped and the dialog falls back to the
arguments alone, as it does for every tool that defines none.

Tool args are validated against InputSchema.json in dispatch() before the
handler runs (tools/_parser/Validate.py), so a bad call comes back as a
sentence the model can fix rather than a traceback. Defaults are not filled
there -- the handler's own args.get(k, fallback) stays the authority.