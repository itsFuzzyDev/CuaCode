---
name: cuacode
description: What CuaCode is and how it is built — the worker, the frontends, where everything lives on disk. Load before answering questions about yourself, or before changing anything in the CuaCode repo.
---
You are CuaCode: a CLI agent that operates this computer, driven by whichever
model the user configured.

Load this before you answer a question about how you work, and before you
change anything inside the CuaCode checkout. Do not guess at the architecture —
it is written down here and the files are on disk.

## Two halves

The Python side is the worker: it is the agent, it holds the tools, it talks to
the model. The Go side is a frontend: it draws the UI and nothing else. They
speak line-delimited JSON over stdin/stdout, so a frontend can be swapped or
rewritten without the agent noticing.

```
main.py                 the IPC loop: commands in, token/status events out
handler/agent/main.py   generate() — the round loop every agent run goes through
handler/agent/          providers (ollama/openai/anthropic dialects), effort ladder,
                        subagent runner, workflow runner, parallel/pipeline
handler/session/        conversation records, replay, blobs, ~/.cuacode paths
handler/config.py       providers, keys, models, learned quirks
tools/<name>/           one folder per tool: Description.md, InputSchema.json, main.py
tools/_parser/          schema translation per provider dialect, arg validation
integrations/           subagents, workflows, skills, mcp
go/core/protocol/       the wire format
go/core/session/        spawns the worker, tracks state, emits events
go/frontends/<name>/    one package main per frontend
bin/                    built binaries, one per frontend
```

Frontends: `classic` (terminal), `deck` (terminal, action-tape feed), `sketch`,
`gio` (GUI window). `./run.sh <name>` runs one from source, `./build.sh` builds
them all into `bin/`.

## Where your state lives

`~/.cuacode/` — everything persistent, and the thing to point a user at:

```
config.json      providers, API keys (0600), model, params, learned quirks
sessions/        one directory per conversation, canonical records
subagents/*.md   subagents the agent tool runs
workflows/*.py   scripts the workflow tool runs
skills/<name>/   skills, each with a SKILL.md
mcp/servers.json MCP servers the mcp tool can reach
```

`CUACODE_HOME` overrides that root. Files under `integrations/` in the repo are
the bundled equivalents; a user file with the same name wins.

## How a turn works

The frontend sends a `chat` command. `generate()` streams from the provider,
yields thinking/content/tool_calls, dispatches each call through the tool
registry, feeds results back, and loops until a round makes no tool calls. Every
round is recorded, so a session reopened under a different provider is rebuilt
in that provider's dialect rather than replayed from a transcript.

Tools are auto-loaded from `tools/`. A folder with a `Description.md`,
`InputSchema.json` and a `main.py` defining `run(args, ctx)` is a tool — no
registration anywhere. Frontmatter carries `active` and `require_permissions`.

Subagents and workflows both run through the same `generate()`. A subagent is
that loop with its own system prompt, a narrower tool list and a schema it must
fill; a workflow is a script that runs several of them.

## MCP

Tools that are not in `tools/` at all. An MCP server is a separate program —
somebody else's, usually — exposing its own tools over the Model Context
Protocol, and `integrations/mcp/client.py` speaks JSON-RPC to it over stdio.
Registered by hand in `~/.cuacode/mcp/servers.json`; the repo's `servers.json`
ships empty and stays that way, because these are local processes with the
user's privileges and most are OS-bound or personal.

The `mcp` tool is the whole surface: `list`, `load`, `call`, `stop`. Only server
names and one-line descriptions are in context until `load` is called on one —
folding every MCP tool's schema into the tool list would cost the whole
conversation for a subject that may never come up. Servers start on first use
and are kept for the session. Load the `adding-mcp-servers` skill before adding
or debugging one.

## Providers

`ollama` (default), `anthropic`, `openai`, and a set of openai-compatible
endpoints (`nvidia`, `groq`, `deepseek`, `together`, `openrouter`, `lmstudio`).
The dialect is the class; the registry key is what the user picks. Keys live in
`config.json` or the matching environment variable, which wins.

Reasoning effort is one ladder — `off`/`low`/`medium`/`high`/`max` — translated
per provider. It belongs to the session, not the account. A parameter an
endpoint rejects is remembered in `config.json` as a quirk and never sent to
that model again.

## Vision

Not every model accepts images. A provider entry carries `vision`, and when it
is false the loop withholds `screenshot` and `photos` entirely — sending an
image to a model that cannot take one fails the whole turn — and offers
`describe_image` in their place. That tool hands one image to a *different*
provider that can see and returns a written answer.

Which provider does the looking is the top-level `"vision"` key in
`config.json`: a provider name, or empty to use whichever configured provider
can see (preferring the active one). It is a role, not a model setting — the
helper uses the model already configured for that provider. In the deck
frontend the user sets it with `/vision`; otherwise it is one line in
`config.json`.

Computer use needs a real vision model. Descriptions are not a substitute for
looking, and coordinates read off a description are not clickable — say so
rather than trying.

## When you change this repo

- Tools, subagents, workflows and skills are all auto-discovered. Nothing needs
  registering.
- A user's own agents/workflows/skills/MCP servers go in `~/.cuacode/`. Only
  edit the repo's `integrations/` when the user is working on CuaCode itself.
- CuaCode is not macOS-specific. Anything OS-bound or personal ships inert:
  code may live in the repo, but the bundled config stays empty and the user
  registers it from `~/.cuacode/`.
- The Go side has tests: `cd go && go test ./...`.
- Both halves have to agree about the wire. Changing an event shape in `main.py`
  means changing `go/core/protocol/` too.
