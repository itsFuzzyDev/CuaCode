# CuaCode

CLI agent for computer use and more.
> [!WARNING]
> This is still a work in progress, use at your own risk (not a virus, just not my problem if you mess up your computer lol).

## Requirements
Have [Go](<https://go.dev/>) installed  
Have [Python](<https://www.python.org/>) installed

### Install
Run the following commands
```bash
# Create a virtual enviroment:
python3 -m venv venv

# If you're on linux/mac
source ./venv/bin/activate
# If you're on windows run a script similar to this, ask your ai to help you if somehow it errors or something lol
\venv\Scripts\Activate

# Install all the requirements (make sure your enviroment is acutally activated)
pip install -r requirements.txt

# Build every frontend **THIS WILL REQUIRE GO**
./build.sh
```

Then run one:

```bash
./bin/classic     # built binary
./run.sh classic  # or straight from source, no build step
./run.sh          # list the frontends you have
```

## Layout

The Python agent is the worker; the Go side is one or more interchangeable
frontends talking to it over line-delimited JSON on stdin/stdout.

```
main.py, handler/, tools/     the Python worker (the actual agent)
go/core/protocol/             the wire format: envelopes, events, subprocess IPC
go/core/session/              frontend-agnostic state: spawns the worker, tracks
                              status/msgs/turns, emits Events
go/core/runner/               finds python + main.py, hands back a started Session
go/frontends/<name>/          one package main per frontend — pick one at run time
bin/                          build output, one binary per frontend
```

Nothing in `core/` knows a frontend exists, and no frontend knows about
another. Adding one never touches the others.

### Frontends

| Name | What it is |
| --- | --- |
| `classic` | Terminal. Full-width scrollback of the streamed worker output, statusline, multi-line input bar pinned to the bottom. Ctrl+C quits, Alt/Shift+Enter for a newline, arrows/PgUp/PgDn/wheel scroll. |
| `gio` | GUI window. Raw wire log — every envelope in and out, prefix tinted by state — with a status strip and an input bar. Enter sends, **Esc stops the run in flight**. Fonts are bundled (`go/frontends/gio/fonts/`); `CUACODE_FONT=/path/to.ttf` swaps the mono face without a rebuild. |
| `sketch` | Terminal scaffold to design on top of. Plumbing is done (worker, session, input, scroll, spinner, Esc cancel); the look is deliberately bare. `main.go` is the wiring, `view.go` is the whole design surface. |
| `deck` | Terminal, built on `sketch`: an action tape rather than a chat log. Every block's text starts in the same column and only the marker to its left changes, so the model's prose is the unmarked baseline and what you asked, what it thought, and what it did to the machine are what catch the eye. Tool calls group under a header listing each call, its arguments and its result, with the error spelled out under any that failed. Prose renders a small subset of markdown (fenced and inline code, bold, italic, headings, bullets, quotes). The status bar carries state, a live timer, the call count and a context gauge. `Ctrl+T` expands thinking, `Tab` collapses the tool calls, `Shift+Tab` spells their arguments out in place instead of summarizing them to a line, `Ctrl+O` opens the call under the cursor in full — every argument as it was sent and the whole result, including the command output and page text the wire only reports the size of (`←`/`→` step between calls, `↑`/`↓` and `PgUp`/`PgDn` scroll, `Esc` closes; it opens on a call that is still running too, where the arguments are the point). `Esc` stops the run. `/` opens the command palette (`/new`, `/provider`, `/effort`, `/permissions`, `/clear`, `/help`, `/quit`) and `@` a fuzzy file picker over the directory you launched from, inserting absolute paths so the agent resolves them the same way wherever it is running. `Shift+Enter` (or `Alt+Enter`) puts a newline in the message instead of sending it. Reopening an earlier conversation is a startup flag rather than a command — `./run.sh deck --resume` to pick one, `--resume <id>` to go straight there — and it is redrawn by replaying its stored records as ordinary events. Asks before the `file` and `shell` calls that change something — a read or a command that only looks runs without a prompt (see Tool permissions); allowing one "for the session" is scoped to that exact thing — a `file` **read**, or that one `shell` command — never the whole tool, and a refusal is always for the single call. The mouse is left to the terminal so selection, copy and paste work normally; scroll with the arrows and `PgUp`/`PgDn`, and bracketed paste arrives as one line. |

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

`Cancel` lands wherever the run currently is: while the request is opening,
between streamed chunks, between tool calls, and inside one. A tool call is
watched from another thread, so a ninety-second `shell` is stopped rather than
waited out — the tool is handed a cancel token and decides what stopping means
for it (`shell` kills its process group), and one that ignores it is abandoned
rather than waited for. A click that has already fired still cannot be taken
back. The worker rewinds the conversation past the partial turn either way, so
no assistant message is left holding tool calls that were never answered.
Completed earlier rounds of the same run are kept.

### Background tool calls

`Background` is the opposite of `Cancel`: the work is wanted, the waiting is
not. The call in flight keeps running under a job id, the agent is handed that
id where it expected a result, and the turn carries on. Nothing is stopped.

It applies to the call in flight and nothing else — sent while the worker is
thinking rather than calling a tool it is discarded, not saved up for whatever
runs next, so offer the key only while the UI shows a tool running.

The agent can also start one itself: a tool declaring `backgroundable: true` in
its frontmatter (`shell`, `agent`, `workflow`, `WebFetch`) gets a `background`
boolean added to its schema, and the loop routes the call to a job instead of
waiting on it. Either way it is the same job, and the agent collects the result
through the `background` tool. When one finishes, a one-line notice naming the
job — not the result — is put into the conversation after the next round of
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

`build.sh` and `run.sh` pick up the new directory automatically — no
registration anywhere. Copy `frontends/classic/` as a starting point if you
want the bubbletea scaffolding.

`sess.Command(action, fields)` sends any of the worker's other commands
(`session.list`, `session.new`, `session.load`, `provider.list`, `provider.use`,
`permission.mode`, `tool.detail`); the reply arrives through the same event
callback, matched by the envelope ID.

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

comes back with `{index, total, name, ts, args, result}` — the arguments the
model actually sent and the whole stored result, strings capped at 20k
characters each and images left as `{"$blob": ...}` refs rather than moving a
megabyte of base64. A call that has been rewound away answers
`{"unavailable": "no such call"}` rather than somebody else's result. `deck`
draws this on `Ctrl+O`; a frontend that never asks sees exactly what it saw
before.

### Tool permissions

A tool declares `require_permissions: true` in its `Description.md` frontmatter
(`file` and `shell` do). Before running one, the worker asks the frontend and
**blocks until it answers** — there is no timeout, so a question left up
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
`preview` — `file` does, for a write or an edit — it draws that instead: the
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
it says yes. It can only ever take a prompt away — a tool without
`require_permissions` was never asked about either way — and it fails closed:
no hook, an exception, anything not understood, and the question is asked as
before.

`file` calls itself safe for `read`, `ls`, `glob` and `grep`, and only outside
the paths whose contents are the thing being protected (`~/.ssh`, `.env`,
keychains, `*.pem`, the agent's own config). `shell` parses the command the way
the shell will — separators, quoting, newlines, substitutions — and calls it
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

Each `session.Event` carries the parsed worker envelope plus a `Snapshot`
(state, msgs, turns, last streamed token, context left). It arrives on the
worker's reader goroutine, so forward it to your own event loop rather than
mutating UI state in the callback.

### Worker discovery

Frontends find `main.py` by walking up from the working directory, then from
the executable. Override with `CUACODE_WORKER=/path/to/main.py`, and the
interpreter with `CUACODE_PYTHON` (a `venv/` next to `main.py` is preferred
over PATH automatically).

## Providers

Ollama is the only provider that is currently set up.
> API keys are also supported with Ollama, you can change your api key in the `main.py:28` file, change the `API_KEY=None` to `API_KEY="Your_api_key"`
## Platform requirements

- macOS: `pyobjc-framework-Quartz`
- Linux: `xdotool`
- Windows: `pywin32`, `psutil`    

## Adding tools

Create a folder under `tools/` with `Description.md`, `InputSchema.json`, `main.py` (export `run(args, ctx)`). Auto-loaded.

