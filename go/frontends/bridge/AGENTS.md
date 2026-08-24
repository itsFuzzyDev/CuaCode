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
              └─ window.__cua.push({events, status, loading})   ← the only way in
                  └─ fold(ev)  turns one event into feed
```

`window.__cua.push` is the **single entry point**. Everything the page displays
arrives through it. Nothing else may mutate the feed - not a timer, not a
callback, not the demo. If you need a new thing on screen, it comes from a worker
event, which means it comes through `fold()`.

Going the other way, the page calls Go through `go('goSend', text)` and friends.
The bindings are declared in `main.go`:

| binding | does |
|---|---|
| `goSend(text)` | send a user message |
| `goSendWith(text, images)` | the same, with `[{name, b64}]` attached |
| `goClipboard()` | → the picture on the system clipboard, or a rejection |
| `goCancel()` | stop the run in flight |
| `goBackground()` | background the running tool call |
| `goCommand(action, fields)` | worker command (`session.list`, `session.load`, …) |
| `goTitle(title)` | name the OS window - the webview does not follow `document.title` |
| `goReply(id, type, fields)` | answer a worker prompt |
| `goReady()` | the page can be evaluated into; flushes what was held |

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

## The rail is information

The hairline down the left gutter is not decoration. It is drawn by the scroller
itself (`.feed` background, `background-attachment: local`) so it is continuous
across the whole run, and each block paints its own stretch to say what kind of
time that was:

| mark | means |
|---|---|
| pink dot | the human spoke |
| dashed | the agent was thinking |
| solid 3px bar | the agent was touching this machine |
| bar amber, breathing | that batch is still running |
| bar green / red | how it ended |

A failed run is findable by scrolling past its red. If you restyle this, keep the
encoding - a colored line that does not mean anything is worse than no line.

Note a block cannot paint outside itself, so the rail's continuity has to come
from the scroller. That is why it is a background and not a pseudo-element.

The other structural claim in the design: **prose is a document, everything else
is an instrument.** The agent's answers are serif at reading measure; thinking,
tool rows, status and the user's own messages are mono. A message to an agent is
a command, so it belongs to the instrument half. Change the faces if you like,
keep the split - it is the fastest way to tell who is speaking.

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
- `?demo&fast` collapses every wait and applies each batch synchronously, so the
  conversation is complete before the load event. Screenshots of it are
  deterministic.

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

- **Live states are not capturable headlessly.** `?demo&fast` finishes before the
  screenshot, so it can only photograph settled states; the amber breathing bar
  and a batch mid-flight can be watched in `?demo` by eye but not captured. If
  you need that, add a fixture stop point that halts the replay with a batch
  still open.
- `ui/fixture.js` covers one short conversation. It does not cover a long
  session, a resumed session, a cancelled run, or a folded/unfolded pass. Extend
  it rather than testing by hand.
- No packaging (`.app` / `.exe` / AppImage) and no native menus. `webview_go`
  provides neither. If those become required, the UI ports to Wails untouched -
  that is the point of keeping the page free of Go-specific assumptions.
- cgo, so cross-compiling needs a runner per OS. Linux needs WebKitGTK present.

---

## Where things are

```
main.go       wiring: flags, window, session, bindings, --serve
pump.go       the one seam between worker and page; coalescing lives here
assets.go     go:embed + the loopback origin
pump_test.go  the coalescing contract
ui/index.html the page shell and its CSP
ui/app.css    tokens, the rail, the two-media split
ui/app.js     block model, fold(), rendering, tool decoding, the demo replay
ui/fixture.js the scripted conversation ?demo replays
```

`ui/app.js` mirrors `frontends/deck`'s block model deliberately - `fold`,
`stream`, `boundary`, `openCalls`, `settle` and the tool-argument formatting are
the same shapes as `deck/feed.go` and `deck/calls.go`. When the worker grows a
new event or a new tool, change both, and read deck's version first: it is the
older and better-tested of the two.
