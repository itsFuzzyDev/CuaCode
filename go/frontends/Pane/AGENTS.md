# pane - notes for whoever works on this next

pane is a new GUI frontend, not `frontends/bridge` with a different skin. It
was copied from bridge and then gutted on purpose: what is left is the shell that
launches the webview and serves `ui/` to it, plus a place to hang bindings on.
The worker, the sessions, the projects and the feed are gone. Add back only what
the new design actually asks for.

## Running it

    ./build.sh Pane                 # or: go build -o bin/Pane ./frontends/Pane
    cuacode pane                    # launcher; the name is matched without case
    ./run.sh pane                   # straight from source

    ./bin/Pane --serve              # print the page's url, no window
    ./bin/Pane -h

`--reload` is the launcher's flag, not this program's: it lives in
`~/.local/bin/cuacode`, which rebuilds and then runs the binary. Nothing here
implements it, so do not add one.

**The directory is capitalised on purpose.** macOS takes an unbundled app's name
in the menu bar off the path of the executable it ran, so `cuacode pane` execs
`bin/Pane` and the menu bar reads Pane. `setProcessName:` does not change that, in
either order relative to NSApplication; appmenu_darwin.go has the measurement. The
launcher resolves the frontend name without case, so pane, Pane and PANE are one
launch, and build.sh names the binary after the directory.

`--serve` is the feedback loop. A webview is a bad place to look at a design
from: nothing outside can open its inspector, screenshot it, or read what it
drew. Served on its own the page is an ordinary local page in any browser.

## The files

    main.go               the launcher: flags, the window, bind()
    assets.go             go:embed ui + the loopback origin that serves it
    windowglass_darwin.go the window as glass; windowglass_other.go is the no-op
    titlebar_darwin.go    the titlebar hand-off; titlebar_other.go is the no-op
    appmenu_darwin.go     the app menu, which is where Cmd+Q and Cmd+H live on
                          macOS; appmenu_other.go is the no-op
    ui/                   index.html, app.css, app.js - the entire design

`bind()` in main.go is the seam. Every call the page makes into Go is declared
there, and the page reaches it through `go()` in ui/app.js, which is a no-op when
the page is served on its own.

## What the page will be fed

Nothing feeds it yet. When the pump lands (bridge's pump.go is the working copy),
the page gets one call per flush, and `window.__cua.push` in ui/app.js is already
where it arrives:

    window.__cua.push({session, project, loading, status, events})

    session   which feed this is; a workspace has several, and the page routes
              each batch to its own
    project   the directory the app is working on, basename of it
    loading   the next session event is a resume, not a new conversation
    status    session.Snapshot, once per batch, never once per event
    events    []wireEvent: everything since the last flush

A wireEvent is `{state, type, token, error, data}`, and `state` is the worker's
own vocabulary, not a second one invented in Go (main.py is where each is sent):

    user           a message the person sent; token = text, data.images = names,
                   never payloads
    notice         something the runtime put in front of the model on the user's
                   behalf (recall, a warning) - drawn, not hidden
    thinking       a chunk of reasoning
    content        a chunk of the answer
    tool_calls     token = the calls the round asked for
    tool_output    token = tool name, data.result = a summary (a body never
                   crosses: output, log, stdout are stripped and counted)
    done           the turn finished
    cancelled      the turn was cancelled
    error          something failed; error = the text
    background     a job was detached; token = job id
    session_title  the conversation got a name

The status-only states carry no token: ready, session, provider, effort,
permission, rate, usage, retry, stopped, cancel_ack, background_ack, deleted,
archived, error. `chat_received` and `chat_queued` are acks with no state at all.

Four things the pump already does, which a page must not assume away:

- **Adjacent `thinking`/`content` chunks are concatenated in Go**, and a merged
event drops `data` (it held only the token). A 900-token answer arrives as ~40
scripts, not 900.
- **`status` is once per batch.** Forty intermediate readings would be forty
copies the page has to skip; `status.LastToken` is the live one.
- **Every event carries its own `status`** - running | tooling | done | cancelled
| error - and that is what drives the coarse state: tooling becomes StateTools,
the rest become the state by name. Do not infer the state from the last event's
`state`; ask what `status` says.
- **`_est` flags mean a number was estimated**, from characters rather than
billed. The worker's own notices mark those with a tilde; the flag is there so a
design can too. Think and reply are counted separately on purpose, so "where did
the ninety seconds go" has an answer instead of one averaged rate.

## The glass

The window is a pane of it, and the tint is the page's: `--app` in ui/app.css
(`#0A0E18`, 60%) on `html` alone. Nothing in the page may be opaque, or it paints
over the glass instead of sitting in it.

Only macOS has it. Everywhere else all three platform files are no-op stubs, the
window stays opaque, and a 60% tint over the webview's own white reads grey rather
than dark, so the page paints `--base` flat instead. Which of the two it paints is
`glassJS()`: the darwin file injects `window.__cuaGlass = true` before the page
loads, ui/app.js turns that into `.glass` on `<html>`, and the stub returns
nothing. An injected script runs at document start, where there is no element to
hang a class on yet, which is why the flag and the class live in two files.

Windows is the next port, and the blocker there is not DWM. DWM will blur behind a
window, but webview_go hands out the window handle and nothing else, so nothing
here can stop the WebView2 painting its own opaque background for the blur to show
through. That means vendoring webview.h, not adding a windows file beside the stub.
Linux has no portable answer at all, because the blur belongs to the compositor.

The other half is windowglass_darwin.go: the window goes non-opaque with a clear
background, the web view is cleared three ways, and it moves into a container so
the NSVisualEffectView is a plain sibling UNDER the page. It cannot be a subview
of the web view, which would draw over the page, and it must not go into the
window's frame view, which is where the traffic lights and the drag live.

Three things that cost a round each to learn:

- **The materials are tints, not radii.** With behindWindow blending the window
  server blurs the backdrop by one fixed amount, and every material is that same
  blur at a different darkness. There is no radius API anywhere: no blurRadius
  key on NSVisualEffectView, no ivar for one, and CABackdropLayer under it
  exposes none either. NSVisualEffectMaterialHUDWindow (13) is the one picked by
  eye against a real desktop; any other is one line away.
- **A non-opaque window loses the system's titlebar drag strip.** That is why the
  page keeps a 28px `#drag` and hands the mousedown back through goDrag. Off
  macOS goDrag is a no-op and the OS strip is still there.
- **A custom blur radius means the app owns the backdrop.** The page draws what
  sits behind the tint and blurs it with `backdrop-filter: blur(Npx)`, which is a
  number the design controls and works everywhere, at the cost of the real
  desktop, which is the one thing the native route buys.

## Colour

The ink is not neutral, and that is the whole finding. The glass is #0A0E18, hue
222; a neutral grey next to it reads dirty rather than dim, so the ramp sits at
the glass's own hue:

    --ink-1  #F2F5FA   text, active icon
    --ink-2  #C3CBD9   icons, labels
    --ink-3  #8E99AC   disabled, metadata
    --ink-4  #5E6879   ghost, hairline

    --line   rgb(255 255 255 / 10%)   separators, edges
    --fill   rgb(255 255 255 / 6%)    raised surfaces on the glass

    --accent #5AA2FF   running, selection
    --tools  #E8B464   a tool call in flight
    --ok     #58D6A8   finished
    --error  #FF7A7A   failed

The design's `#A1A1A1` was kept as `--ink-2`'s lightness for a while and then
measured: 7.5:1 against dark glass, **2.2:1 against bright**. That is the trap of
the tint - it is 60%, so a bright desktop behind it drags the background most of
the way up, and a mid grey has nowhere left to go. Every ink above is chosen
against both ends:

                        dark glass   bright glass
    --ink-1 #F2F5FA      17.6:1        5.2:1
    --ink-2 #C3CBD9      11.8:1        3.5:1
    --ink-3 #8E99AC       6.7:1        2.0:1
    --ink-4 #5E6879       3.4:1        1.0:1

"dark" is the tint over a dark desktop, #0A0E18; "bright" is over a bright one,
which comes out #63676F. `--app`'s alpha is the other lever in the same trade:
60% to 72% pulls the bright end darker and steadies everything above it, at the
cost of seeing less of the desktop.

Depth is white alpha rather than a second grey, so it layers on whatever is
behind the glass instead of guessing at it. The icons cast a shadow in their own
colour rather than black: one `drop-shadow(0 1px 2px)` at 25% of the ink's own
value (`--glow`), so a recoloured icon keeps a shadow that matches it.

## Hard rules

**No framework. No build step. No package manager.** ui/ is three hand-written
files loaded by index.html. No bundler, no node_modules, no JSX, no Tailwind, no
TypeScript. The whole UI ships inside the Go binary through `//go:embed ui`, so
anything that needs a build step cannot get in there.

**Nothing loads from the network.** No CDN, no web fonts, no analytics. The CSP in
index.html blocks it and the app is expected to work offline forever. Fonts are
system stacks (`--mono`, `--read`); if you want a face that is not on every OS,
embed it or pick another.

**One page, one origin.** serveUI() publishes ui/ on loopback behind a random path
token and the window navigates there. Do not add routes or a second page.

## How to check your work

    ./bin/Pane --serve        # open the url it prints

Firefox screenshots it headless, which is how a design gets looked at without a
window:

    firefox --headless -no-remote --profile /tmp/ffprof \
      --window-size=1200,1000 --screenshot /tmp/shot.png "<url>"

The `-no-remote --profile` part matters: Firefox refuses a second instance
otherwise, and it fails by doing nothing rather than by saying so.

Anything that has to sit next to the OS chrome - the traffic lights, the titlebar
- is worth measuring rather than guessing, because the design's canvas is not the
window: its units are 2x, and its traffic lights are drawn about twice real size,
so anything derived from that canvas lands wrong against the real thing. A bare
NSWindow with the same style mask answers both questions in twenty lines (the
buttons are 14x14, their centres 23 apart, the titlebar 32px, and this app's
+10/+8 shift moves them), and a throwaway harness page that draws the lights at
those numbers, plus a magenta rule on their centreline, answers whether the page
lines up with them. Two rounds here went on guesses a probe would have settled.

Nothing in the page may be sized by any of that, mind: sizes are fixed defaults,
and the chrome is only something to clear.

Look at the screenshot before claiming it works. Three real bugs in this
directory's history were invisible in the diff and obvious in the picture.

There are no Go tests left; the ones that came with bridge tested the worker
plumbing. If you add logic worth guarding, add one.

## Known gaps

- No packaging (`.app` / `.exe` / AppImage) and no menus beyond the app menu.
  webview_go provides neither. The native folder picker was hand-bound cgo for
  that reason, and it went out with the rest.
- cgo, so cross-compiling needs a runner per OS. Linux needs WebKitGTK present.
