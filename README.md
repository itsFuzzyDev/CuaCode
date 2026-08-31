# CuaCode

CLI agent for computer use and more.
> [!WARNING]
> This is still a work in progress, use at your own risk (not a virus, just not my problem if you mess up your computer lol).

## Requirements

- [Python](https://www.python.org/) 3.10 or newer
- [Go](https://go.dev/) - only to build or run a frontend

## Install

One script does the whole thing: finds an interpreter, makes a venv, installs
the dependencies, builds every frontend.

```bash
./install.sh
```

Flags: `--no-build` for the Python side only, `--no-venv` to install into the
interpreter as found. Re-running is safe - an existing `venv/` is reused.

Then run one:

```bash
./bin/deck        # built binary
./run.sh deck     # or straight from source, no build step
./run.sh          # list the frontends you have
```

Building on its own is `./build.sh`, optionally with one frontend's name, or
`--keep-going` to report failures at the end rather than stopping at the first
- `gio` needs a C toolchain and platform headers, and the terminal frontends do
not.

### Doing it by hand

```bash
python3 -m venv venv
./venv/bin/pip install -r requirements.txt
./build.sh
```

The interpreter is found at spawn time by layout, not by activation: there is
no need to `activate` anything to run the app. A `venv/` or `.venv/` next to
`main.py` wins over PATH, and `CUACODE_PYTHON` wins over both.

### Platform notes

- **macOS** - `pyobjc-framework-Quartz`, installed for you. Computer use needs
  Accessibility and Screen Recording permission for your terminal (System
  Settings → Privacy & Security), or clicks land nowhere and screenshots come
  back black.
- **Linux** - needs `xdotool` on PATH for clicks, keys and window titles
  (`apt install xdotool` / `pacman -S xdotool`). X11; Wayland is not handled.
- **Windows** - `pywin32` and `psutil`, installed for you. If a frontend starts
  but nothing ever happens, see [Worker discovery](#worker-discovery) - a
  zero-byte `python3.exe` from the Microsoft Store is the usual reason.

## First run

Nothing is configured out of the box. The first launch writes
`~/.cuacode/config.json` (on Windows, `%USERPROFILE%\.cuacode\config.json`) as a
fill-in-the-blank template with a blank entry per provider:

```json
{
  "active": "ollama",
  "providers": {
    "anthropic": {"model": "", "api_key": "", "params": {}},
    "ollama":    {"model": "", "api_key": "", "params": {}}
  }
}
```

Fill in `model` and `api_key` for the one you want and set `active` to its name,
or pick both from inside `deck` with `/provider` - the file is written either
way. It is re-read every turn, so an edit lands on the next message without a
restart. It holds API keys in plaintext and is chmod 0600 on every write (a
no-op on Windows, where the file's protection is the profile directory's).

Everything else the agent keeps lives beside it:

```
~/.cuacode/config.json      providers, keys, models, your permission lists
~/.cuacode/AGENTS.md        your standing instructions, in every conversation
~/.cuacode/sessions/        one directory per conversation
~/.cuacode/subagents/*.md   yours; loaded next to the ones that ship
~/.cuacode/workflows/*.py
~/.cuacode/skills/<name>/
~/.cuacode/mcp/servers.json
```

`CUACODE_HOME` moves the whole root.

## Providers

| Name | Default endpoint | Key read from |
| --- | --- | --- |
| `ollama` | ollama.com | `OLLAMA_API_KEY`, or `ollama signin` |
| `anthropic` | api.anthropic.com | `ANTHROPIC_API_KEY` |
| `openai` | api.openai.com | `OPENAI_API_KEY` |
| `openrouter` | openrouter.ai | `OPENROUTER_API_KEY` |
| `groq` | api.groq.com | `GROQ_API_KEY` |
| `nvidia` | integrate.api.nvidia.com | `NVIDIA_API_KEY` |
| `deepseek` | api.deepseek.com | `DEEPSEEK_API_KEY` |
| `together` | api.together.xyz | `TOGETHER_API_KEY` |
| `lmstudio` | localhost:1234 | `LMSTUDIO_API_KEY` |

The environment variable wins over the key in `config.json`, so a shell can
override a stored key without editing anything. Anything OpenAI-compatible is
one entry in `handler/agent/providers.py`.

`ollama` means ollama.com, not the daemon on your machine, and the model picker
lists that account's catalog rather than `ollama list`. The agent opens every
conversation with roughly 10k tokens of instructions and environment and carries
twenty-odd tool schemas beside it; models small enough to run at home follow
that badly, and the failure is silent rather than loud. Either way in works and
neither needs a key in the file: `ollama signin` through the desktop app leaves
one in `~/.ollama/keys`, which is read when nothing else is set. Point a local
daemon at it through the `lmstudio` entry (any `host:port`, it is just the
OpenAI dialect) if you want to try one anyway.

**Vision is the thing that decides what the agent can do.** A model that cannot
see is never handed the screenshot tools - offered them it would call them, and
the endpoint rejects the image with a 400 that costs the whole turn - so on a
text-only model you get a capable CLI agent and no computer use. Capability is
asked of the provider where it can be, learned from a refusal where it cannot,
and remembered per model. A blind model can borrow eyes: set `vision` to another
configured provider (`/provider` does this too) and `describe_image` routes
screenshots through it.

Thinking effort is per conversation, not per account: `off`, `low`, `medium`,
`high`, `max`, set with `/effort` and stored with the session. Each provider's
own dialect for it - `reasoning_effort`, thinking budgets, `num_ctx` - is
translated from that one ladder.

## Layout

The Python agent is the worker; the Go side is one or more interchangeable
frontends talking to it over line-delimited JSON on stdin/stdout.

```
main.py, handler/, tools/     the Python worker (the actual agent)
integrations/                 subagents, workflows, skills, MCP servers
go/core/protocol/             the wire format: envelopes, events, subprocess IPC
go/core/session/              frontend-agnostic state: spawns the worker, tracks
                              status/msgs/turns, emits Events
go/core/runner/               finds python + main.py, hands back a started Session
go/frontends/<name>/          one package main per frontend - pick one at run time
bin/                          build output, one binary per frontend
```

Nothing in `core/` knows a frontend exists, and no frontend knows about
another. Adding one never touches the others.

### Tools

`WebFetch` `WebSearch` `agent` `app_list` `app_open` `background`
`click` `describe_image` `file` `key` `mcp` `mouse_move` `photos`
`screenshot` `scroll` `shell` `skill` `todo` `type_text` `wait` `workflow`

The pointer-and-keyboard ones (`click`, `key`, `scroll`, `type_text`,
`screenshot`, `app_open`, `app_list`) have a per-OS implementation behind one
`main.py`, picked at call time - nothing OS-specific is imported until it runs.

### Frontends

| Name | What it is |
| --- | --- |
| `deck` | Terminal, the one to start with. Built on `sketch`: an action tape rather than a chat log. Every block's text starts in the same column and only the marker to its left changes, so the model's prose is the unmarked baseline and what you asked, what it thought, and what it did to the machine are what catch the eye. Tool calls group under a header listing each call, its arguments and its result, with the error spelled out under any that failed. Prose renders a small subset of markdown (fenced and inline code, bold, italic, headings, bullets, quotes). The status bar carries state, a live timer, the call count and a context gauge. `Ctrl+T` expands thinking, `Tab` collapses the tool calls, `Shift+Tab` spells their arguments out in place instead of summarizing them to a line, `Ctrl+O` opens the call under the cursor in full - every argument as it was sent and the whole result, including the command output and page text the wire only reports the size of (`←`/`→` step between calls, `↑`/`↓` and `PgUp`/`PgDn` scroll, `Esc` closes; it opens on a call that is still running too, where the arguments are the point). `Esc` stops the run. `/` opens the command palette (`/help`, `/new`, `/provider`, `/effort`, `/model`, `/vision`, `/permissions`, `/clear`, `/quit`) and `@` a fuzzy file picker over the directory you launched from, inserting absolute paths so the agent resolves them the same way wherever it is running. `Shift+Enter` (or `Alt+Enter`) puts a newline in the message instead of sending it. Reopening an earlier conversation is a startup flag rather than a command - `./run.sh deck --resume` to pick one, `--resume <id>` to go straight there - and it is redrawn by replaying its stored records as ordinary events. Asks before the `file` and `shell` calls that change something - a read or a command that only looks runs without a prompt (see Tool permissions); allowing one "for the session" is scoped to that exact thing - a `file` **read**, or that one `shell` command - never the whole tool, and a refusal is always for the single call. The mouse is left to the terminal so selection, copy and paste work normally; scroll with the arrows and `PgUp`/`PgDn`, and bracketed paste arrives as one line. |
| `gio` | GUI window. Raw wire log - every envelope in and out, prefix tinted by state - with a status strip and an input bar. Enter sends, **Esc stops the run in flight**. Fonts are bundled (`go/frontends/gio/fonts/`); `CUACODE_FONT=/path/to.ttf` swaps the mono face without a rebuild. Needs a C toolchain to build. |
| `sketch` | Terminal scaffold to design on top of. Plumbing is done (worker, session, input, scroll, spinner, Esc cancel); the look is deliberately bare. `main.go` is the wiring, `view.go` is the whole design surface. |

### Adding a frontend

Make `go/frontends/<name>/` with a `package main`, and let `runner` do the wiring:

```go
sess, err := runner.Start(func(ev session.Event) { /* hand to your loop */ })
if err != nil { ... }
defer sess.Close()

sess.SendChat("hello")   // user input in
sess.Cancel()            // abandon the run in flight, worker stays up
sess.Background()        // push the running tool call into the background
sess.Snapshot()          // current state out
```

Each `session.Event` carries the parsed worker envelope plus a `Snapshot`
(state, msgs, turns, last streamed token, context left). It arrives on the
worker's reader goroutine, so forward it to your own event loop rather than
mutating UI state in the callback. A worker that dies arrives the same way, as
an `error` status carrying its stderr, so a frontend that draws errors already
draws that one.

`sess.Command(action, fields)` sends any of the worker's other commands
(`session.list`, `session.new`, `session.load`, `session.delete`,
`session.effort`, `provider.list`, `provider.use`, `provider.set`,
`model.list`, `vision.use`, `permission.mode`, `tool.detail`,
`background.list`, `background.kill`, `skill.list`); the reply arrives through the same event
callback, matched by the envelope ID.

`build.sh` and `run.sh` pick up the new directory
automatically - no registration anywhere. Copy `frontends/sketch/` as a
starting point if you want the bubbletea scaffolding.

### Cancelling

`Cancel` lands wherever the run currently is: while the request is opening,
between streamed chunks, between tool calls, and inside one. A tool call is
watched from another thread, so a ninety-second `shell` is stopped rather than
waited out - the tool is handed a cancel token and decides what stopping means
for it (`shell` kills its process group), and one that ignores it is abandoned
rather than waited for. A click that has already fired still cannot be taken
back. The worker rewinds the conversation past the partial turn either way, so
no assistant message is left holding tool calls that were never answered.
Completed earlier rounds of the same run are kept.

### Background tool calls

`Background` is the opposite of `Cancel`: the work is wanted, the waiting is
not. The call in flight keeps running under a job id, the agent is handed that
id where it expected a result, and the turn carries on. Nothing is stopped.

It applies to the call in flight and nothing else - sent while the worker is
thinking rather than calling a tool it is discarded, not saved up for whatever
runs next, so offer the key only while the UI shows a tool running.

The agent can also start one itself: a tool declaring `backgroundable: true` in
its frontmatter (`shell`, `agent`, `workflow`, `WebFetch`) gets a `background`
boolean added to its schema, and the loop routes the call to a job instead of
waiting on it. Either way it is the same job, and the agent collects the result
through the `background` tool. When one finishes, a one-line notice naming the
job - not the result - is put into the conversation after the next round of
tool results.

For a frontend drawing a jobs panel:

```go
sess.Command("background.list", nil)                              // -> {"type":"jobs", ...}
sess.Command("background.kill", map[string]any{"job": "bg_3"})
```

Killing is cooperative, for the same reason cancelling is: a tool that watches
its token stops, and one that does not runs to the end with its result
discarded. The reply says which of the two happened rather than claiming
success either way.

### Reading a tool call in full

A `tool_output` event carries a summary, not the result: a screenshot is a
count, a shell command is a byte total, a fetched page is a character total. The
content is in the conversation, and a frontend drawing one row should not have
to swallow a build log to do it.

What the row cannot show is asked for by index instead. Every `tool_output`
carries the `index` its result was filed under, and:

```go
sess.Command("tool.detail", map[string]any{"index": 4})   // -> {"type":"detail", ...}
```

comes back with `{index, total, name, ts, args, result}` - the arguments the
model actually sent and the whole stored result, strings capped at 20k
characters each and images left as `{"$blob": ...}` refs rather than moving a
megabyte of base64. A call that has been rewound away answers
`{"unavailable": "no such call"}` rather than somebody else's result. `deck`
draws this on `Ctrl+O`; a frontend that never asks sees exactly what it saw
before.

### Tool permissions

A tool declares `require_permissions: true` in its `Description.md` frontmatter
(`file` and `shell` do). Before running one, the worker asks the frontend and
**blocks until it answers** - there is no timeout, so a question left up
overnight is still answered in the morning.

Only frontends that have said they answer are ever asked, so this changes
nothing for a frontend that ignores it:

```go
sess.Command("permission.mode", map[string]any{"mode": "ask"})   // opt in
```

The worker then sends `{"type": "permission", "id": ..., "data": {"name":
..., "args": {...}}}` and waits for `sess.Reply(id, "permission",
map[string]any{"allow": true})`. A refusal comes back to the model as a normal
failed call (`denied by the user`) rather than a missing one, so the turn stays
valid.

The prompt shows what is actually being allowed rather than a summary of it.
`deck` draws the call's arguments in full, and where the tool supplies a
`preview` - `file` does, for a write or an edit - it draws that instead: the
summary line and then the patch itself, with line numbers off the hunk headers,
a marked gutter beside every line that changes and the code syntax-coloured
(Go, Python, JS/TS, Rust, C-family, shell, JSON/YAML, Markdown). A file being
created has no patch to diff against, so its content is drawn as one all-new
hunk. Long blocks are cut to keep the choices on screen and open with `Ctrl+O`.

**Not every call is asked about.** `require_permissions` is a property of the
tool, and per tool is too coarse for the two tools that have it: `file` reads
far more often than it writes, and most shell commands only look at the
machine. A tool that requires permission may define `safe(args, ctx) -> bool`
in its `main.py` and answer for the specific call; the prompt is skipped when
it says yes. It can only ever take a prompt away - a tool without
`require_permissions` was never asked about either way - and it fails closed:
no hook, an exception, anything not understood, and the question is asked as
before.

`file` calls itself safe for `read`, `ls`, `glob` and `grep`, and only outside
the paths whose contents are the thing being protected (`~/.ssh`, `.env`,
keychains, `*.pem`, the agent's own config). `shell` parses the command the way
the shell will - separators, quoting, newlines, substitutions - and calls it
safe only when every segment is a command on a read-only list in a form that
cannot write: `date && whoami && ls -la | grep a` runs without a prompt,
`git log` does, `git push` does not, and `find -delete`, `sed -i`, `sort -o`
and `awk 'BEGIN{system(...)}'` are all recognised as the write they are.
Anything the parser cannot fully account for is asked about.

The lists live in `tools/_safety/`. Personal additions go in
`~/.cuacode/config.json`, so nothing about one machine is checked in:

```json
"permissions": {
  "shell_allow":     ["kubectl", "terraform"],
  "shell_deny":      ["ps"],
  "sensitive_paths": ["/work/vault"]
}
```

### Worker discovery

Frontends find `main.py` by walking up from the working directory, then from
the executable. Override with `CUACODE_WORKER=/path/to/main.py`.

The interpreter is found in this order: `CUACODE_PYTHON`, then a `venv/` or
`.venv/` next to `main.py` (`bin/python3`, or `Scripts\python.exe` on Windows),
then PATH. On Windows a zero-byte candidate is skipped, because `python3.exe`
there is usually the Microsoft Store App Execution Alias - a stub that prints to
stderr and exits, which used to look exactly like the app hanging. If a frontend
draws its chrome and nothing else ever happens, run the worker by hand:

```
venv/bin/python3 main.py          # windows: venv\Scripts\python.exe main.py
```

It should print one `{"state": "ready"}` line and then wait on stdin. Anything
else is the error the frontend was not showing you.

## Extending it

Three ways that need no Go and no restart - files are re-read every turn, so
one written mid-conversation is usable in the next. The ones under
`integrations/` ship with the app; the ones under `~/.cuacode/` are yours, and a
name collision goes to yours.

| | Ships in | Yours in | What it is |
| --- | --- | --- | --- |
| subagent | `integrations/subagents/*.md` | `~/.cuacode/subagents/` | one model run with its own prompt, tools and output schema |
| workflow | `integrations/workflows/*.py` | `~/.cuacode/workflows/` | a script running several of them in a fixed order |
| skill | `integrations/skills/<name>/` | `~/.cuacode/skills/<name>/` | instructions loaded only when needed, by the agent or by `/<name>` |
| MCP server | `integrations/mcp/servers.json` | `~/.cuacode/mcp/servers.json` | tools this codebase did not write |

`integrations/README.md` has the formats. Nothing MCP is registered by default.

A skill is loadable two ways: the agent calls the `skill` tool, or you type
`/<name>` in the palette and the instructions ride along with that message. Its
frontmatter can close either door - `disable-model-invocation: true` keeps it
out of the tool's list, `disable-user-invocation: true` keeps it out of the
palette. Setting both leaves a skill nothing can load, so it is dropped.

### Adding a tool

A folder under `tools/` with three files - `Description.md` (frontmatter plus
the prose the model reads), `InputSchema.json`, and `main.py` exporting
`run(args, ctx)`. It is loaded on the next start, with no registration
anywhere; a folder starting with `_` is a library, not a tool. Optional hooks:
`describe()`/`schema()` for a tool whose options only exist at runtime,
`preview()` for one that can show what a call would do, `safe()` to answer for
a specific call. The `writing-tools` skill walks through it.
