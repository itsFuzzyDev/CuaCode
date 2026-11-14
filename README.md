# CuaCode

An AI agent that runs on your computer and actually uses it.

It reads and writes files, runs commands, opens apps, clicks, types, and takes
screenshots. You watch it work in a terminal or in a window, and you can stop it
any time.

![CuaCode running in a terminal](demo.png)

> [!WARNING]
> Still a work in progress. Not a virus, but back up anything you care about.
> Read a permission prompt before you say yes.

## What it does

Ask it to do something. It plans, uses the tools it needs, and tells you what it
did.

- "find every file over 100 MB in my home folder and tell me which are safe to delete"
- "this test is failing, find out why and fix it"
- "open the invoice pdf and pull the totals into a table"
- "watch that page and tell me when the price drops"

It also remembers. Things that stay true go into `~/.cuacode/memory`, one small
file per fact, and come back when they matter.

## Install

You need Python 3.10 or newer. You need Rust only if you want to build a frontend.

```bash
./install.sh
```

That makes a venv, installs the Python deps, builds the frontends, and puts a
`cuacode` command on your PATH. On Windows use `install.ps1`, and read
[CuaCode on Windows](WINDOWS.md) first.

```bash
cuacode            # start a chat in the terminal
cuacode cuacode    # start it in a window
cuacode schedule   # run jobs on a clock or on a webhook
cuacode --list     # what this copy can run
cuacode --usage    # what your chats have cost
```

Without installing anything: `./run.sh`.

## First run

It ships with no settings. The first launch writes `~/.cuacode/config.json` and
you fill in one model provider:

```json
{
  "active": "ollama",
  "providers": {
    "ollama":    {"model": "", "api_key": "", "params": {}},
    "anthropic": {"model": "", "api_key": "", "params": {}}
  }
}
```

Or just type `/provider` inside it and pick from a list. The file is re-read on
every message, so an edit takes effect straight away. There are a few more keys
in there, `vision`, `effort` and `always_skills`, and all of them are optional.

| Provider | Key |
| --- | --- |
| `ollama` | `OLLAMA_API_KEY`, or `ollama signin` |
| `anthropic` | `ANTHROPIC_API_KEY` |
| `openai` | `OPENAI_API_KEY` |
| `openrouter` | `OPENROUTER_API_KEY` |
| `nvidia` | `NVIDIA_API_KEY` |
| `deepseek` | `DEEPSEEK_API_KEY` |
| `together` | `TOGETHER_API_KEY` |
| `qubrain` | `QB_API_KEY` |

Your key goes in that file in plain text, readable by you only. Anything
OpenAI-compatible can be added in one line.

## Scheduled agents

A schedule is a folder in `~/.cuacode/scheduled/`. The folder name is the job
name. Drop one in while CuaCode is running and the next scan picks it up.

```
~/.cuacode/scheduled/morning/
  job.toml
  PROMPT.md         the task, optional
  SYSTEM.md         standing instructions, optional
  providers.toml    optional
  skills/           optional
  subagents/        optional
  workflows/        optional
  tools/            optional
  mcp/servers.json  optional
```

`job.toml`:

```toml
when = "0 8 * * *"          # or "every 30m", or at = "2026-11-14 09:00"
prompt = """
A short brief for today: weather, calendar, and the one thing I should not forget.
"""
# or keep the task in Markdown:
# prompt_file = "PROMPT.md"
cwd = "~"
tools = ["WebSearch", "WebFetch", "file"]
permissions = "off"         # the only mode an unattended run can have
max_rounds = 10
max_seconds = 600
enabled = true

# hook = true               # POST /hook/<name>, token in .secret
# provider = "ollama"
# model = ""
```

The Markdown form is frontmatter plus the prompt as the body:

```markdown
---
when: "0 8 * * *"
permissions: "off"
---
A short brief for today: weather, calendar, and the one thing I should not forget.
```

`cuacode schedule new morning` writes three files: `job.toml` for the trigger and
limits, `PROMPT.md` for the task, and `SYSTEM.md` for standing instructions.
`prompt_file = "PROMPT.md"` points the job at the task file. The scheduler
detects `SYSTEM.md` on its own and appends it to your `~/.cuacode/AGENTS.md` in
the system prompt. Edit the Markdown, not the TOML.

Each folder adds its own layer over `~/.cuacode`:

- `providers.toml` (or `.json`) adds provider entries. A job can name a provider,
  a model, a key, or a `base_url`, and those stay inside that job.
- `mcp/servers.json` adds MCP servers, same shape as
  `~/.cuacode/mcp/servers.json`.
- `skills/<name>/SKILL.md` adds skills only that job can load.
- `subagents/<name>.md` adds subagents, same format as `~/.cuacode/subagents`.
- `workflows/<name>.py` adds workflows, same format as `~/.cuacode/workflows`.
- `tools/<name>/` adds tools, same folder format as the checkout's `tools/`.
- `SYSTEM.md` (or `AGENTS.md`) adds standing instructions, appended after your
  global `~/.cuacode/AGENTS.md` in the system prompt.

The run receives the user's tools, skills, subagents, workflows, MCP servers,
providers, memory, sessions and `AGENTS.md`, plus the job's `SYSTEM.md`. The
job's `tools = [...]` narrows the tool list over the built-ins plus its own
tools. Omit it for all, `[]` for none.

The rest of the user's home is layered in, not replaced: skills, subagents,
workflows, memory, sessions and `AGENTS.md`. The job file is data. `tools/` and
`workflows/` are code, the same as any CuaCode tool or workflow, and run only
when the agent uses them. Keep them to things you trust.

```bash
cuacode schedule list
cuacode schedule watch          # job added, webhook, tool call, answer
cuacode schedule new morning
cuacode schedule edit morning     # opens PROMPT.md and SYSTEM.md
cuacode schedule run morning
cuacode schedule hook deploy --payload '{"ref":"main"}' --wait
```

`permissions = "off"` is required. A scheduled run has nobody to answer a
permission prompt, so any other value is refused when the folder is scanned. The
worker is also started with `--detached`, and the per-run config says the mode
again.

The daemon scans every two seconds, so adding a folder or editing a file needs no
restart. Runs are serialized: one worker at a time. A daemon that was stopped does
not replay the fires it missed.

Every run is an ordinary CuaCode conversation, named after the job. `schedule
runs` and `schedule show <id>` are the record; `schedule watch` is the live view
and `GET /events` is the same stream. A webhook is loopback-only by default:

```bash
curl -s localhost:8765/hook/deploy \
  -H "X-CuaCode-Token: $(cuacode schedule secret deploy)" \
  -d '{"ref":"main"}'
```

A webhook body can be `{{body}}`, `{{json.ref}}`, `{{header.x-github-event}}`,
`{{now}}` or `{{job}}` in the prompt. A body the prompt does not mention is
appended, labelled as untrusted data.

To keep it running without a terminal open:

```bash
cuacode schedule install-service   # systemd user unit, enabled and started
```

On Linux, `install-service` writes a systemd user unit. On macOS, use `schedule
start`; the scheduler itself is platform-neutral.

## Saying yes

Anything that can hurt you asks first: writing files, running commands, fetching
pages, moving the mouse.

You choose how strict it is:

- `ask` shows you the call and waits.
- `auto` asks a small cheap model first, and only brings you the ones it is unsure
  about. It can flag, it cannot overrule you.
- `bypass` lets everything through except commands that delete things.
- `off` asks nothing at all.

Set it with `/permissions` or in `config.json`. If the filter model errors, you
get asked. It never fails open.

## The screens

| Name | What it is |
| --- | --- |
| `quik` | The terminal one. Small, one row per thing, and the default. |
| `fastrs` | The bigger terminal one. Long tool runs fold into one row you can open. |
| `cuacode` | A window, with your projects and their chats down the side. |

All three can also drive a worker on another machine over ssh, which is what
this calls a box. Add one to `~/.cuacode/hosts.json` and it shows up in front of
you, with that machine's own chats and memory.

## Making it yours

Five things you can drop into `~/.cuacode/` and use on the next message, with no
restart and no rebuild. The ones that ship with the app live in `integrations/`,
and yours win if the names clash.

- **a skill**, `skills/<name>/SKILL.md`: instructions it loads only when they are
  needed, or when you type `$name`
- **a subagent**, `subagents/<name>.md`: one model run with its own job, tools and
  answer shape
- **a workflow**, `workflows/<name>.py`: several subagents in a fixed order
- **an MCP server**, `mcp/servers.json`: tools somebody else wrote
- **a scheduled agent**, `scheduled/<name>/`: a prompt, a trigger, and its own
  skills, MCP servers and providers

A tool of your own is a folder in `tools/` with a `Description.md`, an
`InputSchema.json` and a `main.py`. Nothing to register. The `writing-tools` skill
walks you through it.

## What it keeps

```
~/.cuacode/config.json     your providers, keys and settings
~/.cuacode/AGENTS.md       how you want it to behave, in every chat
~/.cuacode/sessions/       one folder per chat
~/.cuacode/memory/         what it learned about you
~/.cuacode/scheduled/      scheduled agents, one folder each
~/.cuacode/scheduler.toml  webhook bind and port (optional)
~/.cuacode/scheduler/      run records, state and the scheduler log
```

Move the whole thing with `CUACODE_HOME`.

## More

- [WIRE.md](WIRE.md) is the protocol, for writing your own frontend or client
- [rust/README.md](rust/README.md) is the frontend half
- [AGENTS.md](AGENTS.md) is for agents working on this repo, not for people

## License

Free to use, copy, change and share. Not to be sold, and keep the credit. It is
four short paragraphs in [LICENSE](LICENSE).
