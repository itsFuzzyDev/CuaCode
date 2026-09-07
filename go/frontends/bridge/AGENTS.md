# bridge - notes for whoever works on this next

The GUI frontend. Go owns the worker and nothing else; every pixel is HTML and CSS
under `ui/`. If you are redesigning this from a mockup, the design is yours to
change - the machine underneath it is not. This file is the machine.

Read `main.go`'s package comment first, then `pump.go`. They are short.

---

## Hard rules

**No framework. No build step. No package manager.** `ui/` is three hand-written
files loaded by `index.html`. There is no bundler, no `node_modules`, no `npm
install`, no JSX, no TypeScript, no Tailwind. The whole UI ships inside a Go
binary through `//go:embed ui` (`assets.go`) - anything that needs a build step
cannot get in there, and adding one turns a single `go build` into a toolchain.

If a design tool hands you React and Tailwind, **treat it as a picture of the
result, not as code to paste.** Take the tokens, the spacing, the type scale, the
structure. Rewrite the markup as plain DOM and the styles as CSS.

**Nothing loads from the network.** No CDN, no web fonts, no analytics, no
`fetch` to anywhere. The CSP in `index.html` blocks it and the app is expected to
work offline forever. Fonts are system stacks (`--mono`, `--read`); if you want a
face that is not on every OS, embed it or pick another.

**One page, one origin.** `serveUI()` publishes `ui/` on loopback behind a random
path token and the window navigates there. Do not add routes, a second page, or a
router.

---

## The wire, end to end

```
worker (python, line-delimited JSON)
  └─ core/protocol  parses one line into a typed Event
      └─ core/session  applies it, updates the Snapshot, calls notify
          └─ pump.emit   (bridge) queues it, coalesces, one batch per frame
              └─ window.__cua.push({session, events, status, loading})   ← the only way in
                  └─ fold(ev)  turns one event into feed
```

`window.__cua.push` is the **single entry point**. Everything the page displays
arrives through it. Nothing else may mutate the feed - not a timer, not a
callback, not the demo. If you need a new thing on screen, it comes from a worker
event, which means it comes through `fold()`. The one exception is the window
`error` handler, which reports a page failure directly because it has to work
even when the feed machinery itself is broken.

The batch carries a `session` id, because the app is a workspace: several
sessions run at once, each a separate worker process with its own pump, and the
page routes each batch to that session's feed. Every real batch is stamped by
its own pump; an id-less batch (the demo replay) belongs to the active session,
and a batch for a closed session is dropped - the worker's dying flush must not
resurrect its feed. The page keeps one feed per session in the DOM and shows only
the active one - `display:none` does not lay out, so a background session's feed
costs nothing to keep, and switching to it is one layout, not a re-render.

Going the other way, the page calls Go through `go('goSend', text)` and friends.
The bindings are declared in `main.go`:

| binding | does |
|---|---|
| `goSend(text)` | send a user message to the active session |
| `goSendWith(text, images)` | the same, with `[{name, b64}]` attached |
| `goClipboard()` | → the picture on the system clipboard, or a rejection |
| `goCancel()` | stop the run in flight |
| `goBackground()` | background the running tool call |
| `goCommand(action, fields)` | worker command (`session.list`, `session.load`, …) |
| `goLoad(id)` | open an archived conversation in the active session: flips the pump's loading flag, then `session.load` |
| `goPickFolder()` | native folder picker (NSOpenPanel, darwin); → the path, or "" on cancel |
| `goTitle(title)` | name the OS window - the webview does not follow `document.title` |
| `goReply(id, type, fields)` | answer a worker prompt |
| `goReady()` | the page can be evaluated into; flushes what was held |
| `goNewSession(dir)` | start a fresh worker, make it active; → its id. Empty dir = the launch directory; a dir makes it the session's own working directory and project |
| `goSwitch(id)` | make a session active |
| `goClose(id)` | close a session; → the next active id |

Always call them through the `go()` helper, never `window.goSend(...)` directly.
Served on its own the page has no bindings, and `go()` is what lets it still run.

---

## Invariants a mockup will not tell you

These are the ones that break silently. Nothing on screen looks wrong until a
session gets long, and then everything does.

**1. Append, never re-render.** A streamed block owns one text node and grows by
`textNode.appendData(chunk)`. Do **not** rebuild a block's markup per chunk -
`innerHTML = render(...)` on every token is quadratic in the length of the
message, which is exactly the lag that gets worse the more the model says. The
one place markup is generated is `settleProse()`, once, after a message is
finished.

**2. One frame, one layout.** `__cua.push` queues; `apply()` runs inside a single
`requestAnimationFrame`, however many batches landed. Do not touch the DOM
outside that path.

**3. Go coalesces before JS sees anything.** `pump.append` concatenates adjacent
chunks of the same stream and `flushEvery` (24ms) is one batch. A 900-token
answer reaches the page as ~40 scripts, not 900. `pump_test.go` guards this; if
you change the merge, that test is the contract.

**4. No idle work.** An idle window schedules nothing - no animation loop, no
polling, no interval. The one timer (`tickIfBusy`) starts when a batch of calls
is open or the worker is busy, and stops when it is not. Keep it that way.

**5. No `content-visibility: auto` on `.b`.** It was tried and removed. Skipping
offscreen blocks needs `contain-intrinsic-size: auto <len>` to remember what each
block measured; where that is unsupported, every offscreen block collapses to
zero height, which keeps it offscreen - the feed empties itself the moment it
outgrows the window. The comment in `app.css` says so; do not put it back without
testing a conversation taller than the viewport.

**6. Scrolling is instant.** Never `scroll-behavior: smooth`. A smooth scroll
animates the reader toward a position the stream has already left, so the view
sits permanently behind the text it is following.

---

## Never build these (Boaz's standing order)

Do not add, propose, or sneak in any of:

- Drag-to-reorder of tree rows or groups. Reordering exists, but as explicit
  up/down arrows on promoted and pinned group heads (`project.move`) - never
  as a drag gesture, and never for session rows (their order is recency and
  pins).
- Right-click context menus.
- Live multi-window sync of any kind (polling, push channels, refresh timers -
  every refresh rides a user action, and the no-idle-work rule stays).
- Keyboard navigation of the tree itself (the palette covers keyboard access).
- Fuzzy correction for `#project` scoping.
- User-resizable sidebar widths. The sidebar has set sizes only: 268px out,
  88px rail in (88 so the native lights fit without clipping), and the rail
  is forced below a 800px-wide window.
- Project details beyond name + icon. The icon is one of: a single emoji, a
  gradient circle from the fixed set ("grad:<n>"), or an uploaded image
  (goPickIcon + canvas downscale to 64px, stored as a data URL, capped at
  96KB). No free-form color pickers, no per-project notes.

If a request seems to want one of these, stop and ask Boaz instead of building
a version of it.

---

## How to check your work

This frontend can be looked at without a worker, a window, or Python:

```sh
go build -o bridge ./frontends/bridge && ./bridge --serve
# prints two urls: the page, and the page with ?demo
```

- `?demo` replays a scripted conversation (`ui/fixture.js`) through the real
  `window.__cua.push`, at real speed. Open it in a browser to watch streaming,
  the live amber bar, and results settling.
- `?demo=name` picks a scenario: `long` (a session long enough to scroll),
  `resumed` (the reopened-session banner and a names-only replay), `cancelled`
  (a run stopped mid-tool), `folded` (thinking unfolded, calls
  folded), `workspace` (two sessions at once, switched between, so the tabs and
  per-session feeds show).
- `?demo&fast` collapses every wait and applies each batch synchronously, so the
  conversation is complete before the load event. Screenshots of it are
  deterministic.
- `?demo&stop` halts at the first batch marked `stop` in the fixture, leaving a
  batch of calls open - the one live state a finished replay cannot show. This
  is how the amber bar and pending rows are photographed.

Headless screenshot:

```sh
firefox --headless -no-remote --profile /tmp/ffprof \
  --window-size=1200,1000 --screenshot /tmp/shot.png "<url>?demo&fast"
```

The `-no-remote --profile` part matters - Firefox refuses a second instance
otherwise, and it fails by doing nothing rather than by saying so.

**Look at the screenshot before you claim it works.** This is the whole reason
the GUI is HTML: three real bugs in this file's history were invisible in the
diff and obvious in the picture. A prior toolkit attempt failed for exactly the
lack of this.

Also run `go test ./frontends/bridge/` - it covers the coalescing, the held-until-
ready behaviour, and the batch JSON shape.

Uncaught JS errors are written into the feed as a notice, because nobody can open
an inspector inside the app's window. If the feed stops growing, look there.

---

## Known gaps

- No packaging (`.app` / `.exe` / AppImage) and no native menus. `webview_go`
  provides neither (the folder picker is hand-bound cgo for that reason). If
  those become required, the UI ports to Wails untouched -
  that is the point of keeping the page free of Go-specific assumptions.
- cgo, so cross-compiling needs a runner per OS. Linux needs WebKitGTK present.
- The folder picker and the titlebar shift are darwin-first; the other
  platforms have no-op stubs.

---

## Where things are

```
main.go            wiring: flags, window, bindings, --serve
titlebar_darwin.go the window's top hand-off: titlebar goes transparent and
                   full-size, so the native lights sit on the page's sidebar
                   (see below); titlebar_other.go is the no-op elsewhere
picker_darwin.go   the native folder picker (NSOpenPanel); picker_other.go stubs it
workspace.go       the set of open sessions; each a worker process with its own pump
pump.go            the one seam between worker and page; coalescing lives here
assets.go          go:embed + the loopback origin
pump_test.go       the coalescing contract
ui/index.html      the page shell and its CSP
ui/app.css         tokens, sidebar and tree; the feed is bare (chat ui dropped, redesign pending)
ui/app.js          block model, fold(), rendering, the project tree, tool decoding, the demo replay
ui/fixture.js      the scripted conversations ?demo replays
```

The sidebar carries the app's own chrome: the wordmark and pill, a collapse
chevron, a search row (opens the palette), and the project tree - projects
from each session's own spawn directory and the store's `cwd`s, filtered to
the promoted projects (projects.json), the current cwd, and live sessions -
and under each project the sessions that belong to it, live ones first, each
row carrying its state dot. The sidebar is either full width or the 76px rail
(toggle chevron; forced below 800px), never anything between. macOS gives the
traffic lights back as an overlay: `styleTitlebar` (titlebar_darwin.go) makes the native
titlebar transparent and full-size-content, dark background and appearance, so
the lights render over the sidebar's first row. Three consequences are wired
together: the brand row (`body.native .brand`) steps right of the lights and is
hidden inside the narrow-width layout, where the strip of window above belongs
to the OS alone; the wordmark never draws its own lights, because they are real
Buttons and close the window.

`ui/app.js` mirrors `frontends/deck`'s block model deliberately - `fold`,
`stream`, `boundary`, `openCalls`, `settle` and the tool-argument formatting are
the same shapes as `deck/feed.go` and `deck/calls.go`. When the worker grows a
new event or a new tool, change both, and read deck's version first: it is the
older and better-tested of the two.

---

## The sidebar's rules of motion

These are Boaz's calls, not defaults - do not "simplify" them away.

- **The row dot is a notification, not a label.** Amber and breathing while
  that session's worker runs; green when a run *finished* there and nobody has
  looked since (cleared by opening the session); archive rows carry no dot.
- **Row recency is `lastAct`.** It moves only on a user message and on a run
  finishing - never on streaming, thinking, or tool calls, or the sidebar
  reshuffles every time the model breathes.
- **Pins persist.** A pinned row floats above its project's other sessions,
  oldest pin first, and holds its place regardless of activity. Both kinds
  persist now: session pins are a ts on the conversation's meta (worker
  command `session.pin`, reply re-stamps the frontend), project pins a name
  list in projects.json (`project.pin`). A set pin renders always - it is
  state, not an affordance. In the rail the pins do not render; the rail
  keeps the tree's order (pinned groups first), so nothing lies about what
  sits on top.
- **The groups hold their places.** Pinned projects first, in pin order; then
  the project this window works in; then promoted projects in promotion order
  (projects.json); then everything else alphabetically. The recency/name
  toggle orders the sessions inside each group and never moves a group - the
  tree must not reshuffle as sessions open and age.
- **Project details are a display name and one emoji.** The pencil on a
  promoted group's head opens an inline editor; the tree freezes under it
  (Enter commits via `project.rename`, Esc cancels). The display name and
  emoji render in tree, rail, palette and header, and `#scope` matches both
  the custom name and the basename - but the group key stays the basename,
  so pins and promotion never detach from their group.
- **The tree only animates on the user's own action.** Interactive paths set
  `sideMotion`; the next rebuild glides moved rows/groups from their old spots
  (FLIP via `--flip`) and fades in new ones, staggered. Batch redraws -
  streams, dots, titles - never set it, so nothing moves mid-stream, and
  `?demo&fast` never animates (deterministic screenshots). All of it dies
  under prefers-reduced-motion.
- **The navbar and window title belong to the session on screen.** A
  background session's name or project never draws them; only `switchTo` does.
