# Subagents, workflows and skills

Three ways to extend the agent without touching its code. A **subagent** is one
model run with its own system prompt, its own tools and a schema it has to
fill. A **workflow** is a script that runs several of them in a fixed order. A
**skill** is instructions the agent loads when it needs them.

All three load from two places: the ones here ship with the app, the ones in
`~/.cuacode/` are yours, and a name collision goes to yours. Everything is
re-read every turn, so a file written mid-conversation is usable in the next
one — no restart.

    integrations/subagents/*.md      ~/.cuacode/subagents/*.md
    integrations/workflows/*.py      ~/.cuacode/workflows/*.py
    integrations/skills/<name>/      ~/.cuacode/skills/<name>/
    integrations/mcp/servers.json    ~/.cuacode/mcp/servers.json

A fourth way, `integrations/mcp/`, reaches tools this codebase did not write at
all — see the section at the end.

## Writing a subagent

Frontmatter for the machine, body for the system prompt — the same shape a
tool's `Description.md` uses.

```markdown
---
name: reviewer
description: One line. The main agent reads this to decide whether to call you.
tools: [file, shell]      # names from tools/. [] means none. "*" means all.
effort: low               # off | low | medium | high | max
max_rounds: 8             # hard stop; it returns stopped: "max_rounds"
output:                   # a JSON-Schema subset -- see tools/_parser/Validate.py
  properties:
    verdict: {type: string}
    issues:
      type: array
      items: {type: string}
  required: [verdict]
---
The system prompt. Say what done looks like and what not to do.
```

`output` is what makes it callable code instead of prose. The agent is handed a
`submit_result` tool built from that schema, and it is the only way back —
anything else it writes is discarded. A failed validation returns to the agent
as a tool result, so it corrects itself on the next round.

Leave `output` off and the agent's final text comes back as a plain string.

Two things to keep in mind while writing the prompt. A subagent starts cold: it
sees the prompt string and nothing else, not the conversation it came from. And
it cannot ask anything — an underspecified job returns a confident wrong answer
rather than a question.

## Writing a workflow

A plain Python file with a `run(args)`. No imports: `agent`, `parallel`,
`pipeline`, `log` and `AgentSpec` are already in scope.

```python
NAME = "audit"
DESCRIPTION = "What this does and what args it wants. The model reads this."

def run(args):
    files = args.get("files") or []
    log(f"reviewing {len(files)} file(s)")
    found = pipeline(files, lambda f, _item, i: agent("reviewer", f"Review {f}"))
    return {"issues": [x for r in found if r for x in r.get("issues", [])]}
```

- `agent(name_or_spec, prompt, **overrides)` returns the agent's output alone,
  or `None` if it failed. Overrides are AgentSpec fields: `effort="high"`,
  `max_rounds=20`.
- `pipeline(items, *stages)` runs each item through every stage independently —
  no barrier, so one slow item does not hold up the rest. Stages are called
  `(previous, original_item, index)`.
- `parallel(thunks)` is the barrier version. Use it only when a stage genuinely
  needs every earlier result at once: dedup across the whole set, an early exit
  on a count, a synthesis that compares findings to each other. "I need to
  flatten the list first" is not that — flatten inside a stage.
- A failed agent or stage is `None` in the results, never an exception. Filter.
- `MAX_AGENTS` (60) is a backstop against a loop whose exit condition never
  became true, not a budget.

`integrations/workflows/research.py` is the worked example.

Workflows are Python and they run with your privileges. Do not install one you
have not read.

## Writing a skill

A folder with a `SKILL.md`, plus whatever else that skill needs.

```markdown
---
name: deploying-web
description: One line. When does this apply? The agent reads only this until it loads the skill.
---
The instructions, addressed to the agent doing the work.
```

Only the name and description are in context by default; the body is read when
the agent loads it. So fifty skills cost fifty lines, not fifty documents, and
the description is the part that decides whether a skill is ever used.

Two things can load it: the agent, through the `skill` tool, and the user,
by typing `/<name>` — the palette lists every skill under the built-in
commands, and the instructions ride along with that message. Frontmatter can
close either door:

```markdown
disable-model-invocation: true   # not offered to the agent; /<name> only
disable-user-invocation: true    # not in the palette; the agent's to reach for
```

Both default to false. Setting both would leave a skill nothing can load, so
that folder is dropped rather than listed as installed.

Other files in the folder — scripts, templates, reference tables — are listed
on load but not read. The body points at what to read and when; the agent gets
the folder's absolute path back and reads or runs them itself. A deterministic
script beats a paragraph asking the agent to be careful.

## Registering an MCP server

The other three extend the agent with things written here. An MCP server is a
program written somewhere else entirely — a desktop app's controller, a
database, an internal API — that exposes its own tools over the Model Context
Protocol, and CuaCode talks to it as a client.

```json
{"mcpServers": {"name": {"command": "python3", "args": ["/abs/path/server.py"],
                         "description": "One line. The agent reads this.",
                         "platform": "darwin"}}}
```

`integrations/mcp/servers.json` ships empty and should stay that way unless a
server belongs to everyone on every OS: these are local processes running with
your privileges. Yours go in `~/.cuacode/mcp/servers.json`, which wins on a name
collision, and the key is spelled the way Claude Desktop spells it so blocks
paste between them.

The agent sees a server's name and description, nothing more, until it loads
one — same bargain as skills. `integrations/mcp/README.md` is the protocol, the
config fields, and the bundled Spotify server as a worked example.

The agent can write all three of these itself: the bundled `writing-subagents`,
`writing-workflows` and `writing-skills` skills are the formats, and `cuacode`
is what it knows about this codebase.
