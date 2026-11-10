# Working on the Rust half

Three frontends over one worker, which is the Python process in the parent
directory: `quik` draws one conversation in a terminal and is the one in daily use,
of an orchestrator: bin `cuacode`, it reads the state root itself and drives one worker.
All three are built on the shared half below.

The `app` crate is meant to be the app: projects and the sessions standing in
each, the conversation, and a composer that sets provider, model, permissions and
location. It is parked for now; the terminal frontend is where the work goes.
`README.md` says what this is; this file is the rules. Read both before the first
change, and neither is long.

## First, the test command

```bash
cargo test --workspace
```

A bare `cargo test` here tests only the root package, so fastrs's and app's tests
do not run at all. `--workspace` is what reaches them, and it links the gpui
stack in the test profile for `app`, which takes minutes the first time and is
the reason `target/` is not a build cache but a build cost, 8.9GB of it for a
cold full-workspace build. That is the price once; every run after it is
incremental.

`cargo check` answers "does this compile" without linking a binary, and it is
what an agent should run while it is only reading its own edit back.
`cargo build`/`cargo test` are for when the binary or the run is the point.
Nothing here is worth 9GB of someone's disk to prove a type.

The size is held down at the source by `[profile.dev] debug =
"line-tables-only"` in `Cargo.toml`. Cargo's default is `debug = 2`, full DWARF
on every dependency, and that is what put this tree at 38GB: the debuginfo is
~90% of an `rlib` and nothing here ever points a debugger at one. A panic
backtrace keeps its file:line under line tables, which is the only thing that
was being read. Do not put `debug = 2` back, and do not add a per-crate
override for it -- the one workspace setting covers every frontend, including
the ones not written yet.

When the disk does still need the room back, delete the profile, not the
cache: `cargo clean --profile dev` drops `target/debug`, `cargo clean --profile
release` drops `target/release`; the next build relinks from crates.io, which
costs minutes once. Never delete `target/` while a frontend is running from it,
and never `rm -rf` it -- `cargo clean` is what knows which profile owns which
file.

## One job per file

- `app/state.rs` - the state root, read. `projects.json` and
  `sessions/*/meta.json` as `handler/session/store.py` writes them, the
  session list grouped under its project, a transcript's tail, and the fold that
  turns records into the rows a person reads. Owns no drawing, and runs no
  worker: opening a process to hear it read a JSON file buys nothing but a
  process.
- `app/view.rs` - what the shell is made of: the chrome, the rail, the
  transcript's rows, and the composer. Draws, and owns no state.
- `app/theme.rs` - the palette and the numbers. Superset's `ember`, quoted from
  their own `globals.css` and `ember.ts`, opaque. Draws nothing.
- `app/motion.rs` - the hover clock, the pulse, and the arrival curve a menu
  rides. Reads the clock at paint time.
- `app/main.rs` - the window, the keys, the four composer menus, and the cache.
- `app/agent.rs` - one worker, on this machine or on a box, and the little that is
  live rather than on disk: busy, phase, error, a permission ask, and the listing a
  box's worker answers `session.list` with.
- `src/wire.rs` - the envelope and its fields. The format, and nothing else.
- `src/worker.rs` - find python and `main.py`, spawn, read lines, reap.
- `src/hosts.rs` - `~/.cuacode/hosts.json`: the boxes a worker can run on, how
  `ssh` is handed the worker, and the one command that reads a file out of a box's
  own state root.
- `src/session.rs` - the conversation's state, folded from events, plus sending.
- `src/transcript.rs` - events folded into rows: the model a UI draws.
- `src/code.rs` - a fenced line read as the tones a terminal draws code in: what
  the fence's language says a comment is, and what the line before it left open. Both
  TUI frontends map its tones to their own palette; a second vocabulary in a frontend
  is how two panes end up disagreeing about what a comment looks like.
- `src/console.rs` - the console's input mode, kept the way a frontend needs it, and
  the filter that keeps a terminal sequence out of the message being typed. The mode
  belongs to the console, not to a process: anything else on that console can set it,
  and with `ENABLE_VIRTUAL_TERMINAL_INPUT` on, the pointer's moves arrive as
  characters and the keyboard's keys stop matching. `Claim` reasserts it every pass;
  `Typing` is the second door, for the sequence that got in anyway.
- `src/version.rs` - the build stamp `build.rs` gives it, and the one question
  about origin that answers "is this code out of date". Fetches, so it is asked
  on a thread and never from a frame.
- `src/doctor.rs` - `cuacode doctor`: the build, the tree, and how far behind it
  is; `--upgrade` is the same answer acted on: fetch the branch the remote names
  as its default, fast-forward onto it, then rebuild. It does not use the local
  branch's upstream, which a fork can leave pointing at the fork. Its tiny
  `cuacode-doctor` binary keeps Windows from having to overwrite a running
  frontend. Prints and exits, before any window is made.
- `quik/view.rs` - what a row looks like in cells, and `ink`, quik's only palette.
- `quik/keys.rs` - every global key as a named action, and `~/.cuacode/keys.json`.
- `quik/main.rs`, `quik/menu.rs` - the window, the keys, and the `/`, `$`, `@` menus.
- `fastrs/view.rs` - what a row looks like on the page, in cells.
- `fastrs/menu.rs` - the `/`, `$` and `@` menus: the rows, the filter, and
  what choosing one writes onto the message. The calls list behind ctrl-o is the
  same menu with the conversation's own calls in it, so it is a picker: it opens
  apart from the line and chooses by opening a row rather than writing one.
- `fastrs/main.rs`, `app/main.rs` - keys, layout, wiring, the message being
  typed and its caret, and nothing else.

The shared half draws nothing and a frontend owns no conversation logic: the
terminal frontend must not link a UI framework, and the GUI must not link a
terminal library. A new frontend is a crate under `rust/` with a `[[bin]]`; the
shell scripts pick it up from the manifest with no other change.

## The app's own rules

- **A box is a state root, not a label.** `~/.cuacode/hosts.json` names machines a
  worker can run on, and one is chosen at a time. Changing it swaps everything the
  screen is a view of: selection, cache, transcript, and the rail, which comes from
  that worker's own `session.list`. A path is never carried across: the worker
  stamps the directory on its own sessions, `agent::set_cwd` refuses a local one
  when the worker is remote, and `session::terminal_for` drops the cwd from the
  envelope for the same reason. The bug this replaced put a Mac path on a session
  on a Linux box. A frontend that draws from a disk fetches that box's file with
  `Host::read_command` on its own thread (`ask_box`/`apply_box`), never in a frame
  and never on the tick.

  A box's worker is **hosted** on that box, and this frontend is a client of it
  (`Worker::attach`): it runs in a tmux pane there, commands go in with `send-keys`,
  and events come off the end of `~/.cuacode/live/cua.log`. Nothing here is its
  parent, so `ctrl-d`, a quit, or this machine going down ends the view and never
  the work - a turn in flight keeps generating. The worker is started as
  `main.py --detached`, which forces the permission mode to `off`: `bypass` still
  stops for an irreversible call and puts it to a person, and that person may be on
  a plane.

- **The render path does not touch the disk.** `render` runs thirty times a second
  while a hover is travelling, and a frame that opens a file is a frame the
  pointer feels. `app/Cache` reads on a stamp of `(length, mtime)` from the
  window's own clock, and every path that moves the selection re-reads. This is
  measured, not a preference: the old reader parsed a whole `messages.jsonl` per
  frame, which on a 2.8MB session is 0.32s of CPU for every second of hovering.
  The reader takes **the tail** - `TAIL_BYTES`, then the partial line it landed in
  is dropped, or half a JSON object parses as nothing and a row vanishes.
- **Two naps, not one.** 33ms while a blend is travelling or a run is in flight;
  250ms when nothing is. A settled window still has to notice a session being
  written to, and four polls a second is enough to notice that with.
- **A run is one row.** Records are not rows: forty calls between one answer and
  the next fold into one line saying `5 calls · read, edit`, which opens on a
  press. `state::fold` is the only thing that decides this, and it is cached with
  the transcript, not run per frame. A frontend that draws records directly is
  the wall of lines the fold exists to prevent.
- **A list the window offers comes from the source that owns it.** The provider
  registry is read by asking the worker (`python3 -c`, 77ms, offline, no key), not
  by keeping a second copy in Rust: a provider list built from `config.json` alone
  said *one provider* while the worker could reach nine, because a key is reachable
  from either the file or that provider's own environment variable
  (`handler/config.py:api_key` reads both). The models come from the account's own
  history first, newest first, then the registry's shortlist, then
  `model_caps`. No hand-written list of either.
- **A result is a JSON value, not a string.** `messages.jsonl`'s `result` is a
  string for some tools and an object for others
  (`{"ok": true, "app": "Firefox"}`); reading it as a string drew an empty row for
  every object-shaped one, which is most of them. `state::result_line` renders and
  flattens it.
- **Nothing is offered that cannot be done.** The permission modes are the
  worker's own four (`handler/config.py`'s `ask | auto | bypass | off`), the
  providers and models come out of `config.json`, and the location chip locks the
  moment a session is selected, because that is the worker's own rule: `set_cwd`
  only takes a value before the first record. A locked control says it is locked
  rather than going quietly dead.
- **The palette is quoted, not re-derived.** `app/theme.rs` carries superset's
  literals and the file they came from. It may not drift into a second palette.

## Rules

- **One definition per thing.** `view::pieces` is the only code that says what a
  row looks like, and `--print` and the TUI both go through it. `fastrs::menu`
  is the only command list and the only key list. A second copy of any of these
  is the bug, not a convenience.
- **An absence is not a zero.** Counters arrive as `Option` because the worker
  leaves one out when it has nothing to report, and a zero reads to a frontend
  as a measurement. Never default a missing number to 0 to make the code shorter.
- **Invariants that have tests, and must keep them:**
  - two answers can never merge into one row: streamed text carries the block it
    belongs to
  - the user's own line is drawn by the frontend, because a live turn is never
    echoed back by the worker
  - `state` and `status` are different keys on the same envelope
  - the `tool_calls` token arrives in the *provider's* dialect, and there are
    three of them
  - in fastrs a newline lands at the caret rather than at the end of the
    message, and the `/` menu is filled twice (the local verbs when it opens,
    the worker's list when it arrives) with the second fill keeping the first
  - a row keeps the spaces it was given: the wrap takes the single space a break
    falls on and nothing else, so a code line's indentation and a code block's
    padding survive being drawn on a row
- **The wire is a contract with the Python side.** Break `main.py`'s output and
  the fixture is what tells you: `python3 fixtures/record.py ../main.py <id>`
  re-records it, `cargo test` holds the types to it. Never hand-edit the fixture;
  it is a recording, and an edited one proves nothing.
- **Nothing on screen unless it earns the line.** No shortcut list, no message at
  startup, no "just in case" banner: they are annoying, and `/help` is the one
  door. A row of keys in the corner of every frame is the thing that was asked to
  have removed. That door is a list of keys and not of commands: the commands are
  the `/` menu's own list, and naming one in the panel while the other twenty are
  missing is the kind of thing that reads as a bug.
- **Mark a deliberate ceiling** with a `ponytail:` comment naming it and the
  upgrade path. `src/worker.rs` and `src/transcript.rs` have the pattern. An
  unmarked short-cut reads as an oversight and gets "fixed" into a bug.
- **A menu writes text; it does not send a command.** `/name`, `$name` and a
  path are put on the message and the worker parses the finished line, so the
  command table and the skill list keep one owner each. A frontend dispatching
  its own actions would be carrying a second copy of both. Three things are the
  frontend's own and are the exceptions, all documented where they live: a
  picker (`provider`, `model`, `permissions`, a session) switches rather than
  writes, and `/help` and `/permissions` typed whole are read in fastrs's send
  path, because the worker has no command for either and the alternative is
  prose about keys going to the model.
- **Do not invent a number.** If the wire did not say how long something took,
  how many tokens it cost, or whether it worked, the row says nothing about it.
- **A prompt waits for the keyboard to go still.** `main.rs`'s `raise_ask` is the
  only place a permission or a question becomes the panel: an envelope that arrives
  while someone is typing is held, not drawn, and goes up once the last key is
  `ASK_AFTER` old. The worker is blocked on the answer either way, so the wait costs
  nothing but the wait, and the field keeps working while it waits, so the message
  being typed is still there when the panel goes up. A frontend that set the panel
  straight from `feed` is the bug this replaced.
- **A permission is a decision, not a y/n line.** The same panel a question gets,
  with two rows (`allow`, `refuse`), each naming the keys that send it. Above them
  the call as the row names it and, under that, the reason the gate stopped it --
  what is being allowed first, because that is the thing being decided, then why it
  was stopped. `view::ask_body` is the document and `ask_panel` lays it out; the field
  is empty while it is up, because the field is not where it is answered. Nothing on
  that panel is labelled: a heading over one sentence is a form, and a diff is a diff
  by its colours. The panel is as wide as its content, not as wide as the pane. The
  document scrolls under the rows with pgup/pgdn/home, because a diff cut off at the
  pane is the thing a person deciding on a write has to reach.
- **A permission says nothing after it is answered.** The call's own row is already on
  the screen and the result lands against it, so a notice under it saying `allowed
  shell` was the same fact twice, and in the one row of the transcript with no mark to
  carry. A question still says its answer, because it is the only record of it.

## Traps

- The composer is **one card**, and that is superset's own `ComposerBody`:
  `rounded-2xl bg-card ring-1 ring-border`, the editor in `px-4 pt-3.5 pb-1`, the
  footer in `flex min-h-12 items-center gap-2 px-3 pb-2.5`. A bordered field with
  a row of bordered chips under it is what reads as a form and not as a composer.
  Their chips are `PickerTrigger`, which is `variant="ghost"` - a label and a
  chevron, no border at rest.
- `Menu` is gpui's menu-bar type. A local enum of that name shadows it and
  `Menu::new("File")` stops resolving. The app's own is `List`.
- `cx.defer` takes a **one-argument** closure (`&mut App`), not a context. It was
  meant to be the deferral of a disk read out of `render`; every real path already
  syncs the cache, so `app/Cache::sync` is called from the click and the clock and
  `cache_for` is a plain read.
- `track_scroll(&handle)` is what makes the wheel behave in a scroll container.
  Without a handle there is nowhere for the offset to live between frames and the
  page re-derives it from the pointer, which is the "scrolling feels wrong"
  symptom.
- **A `TextRun` background has no padding and no corner.** Inline code drawn as a
  run comes out as a highlighter band hugging the glyphs, so a paragraph is laid
  out as its words instead, one item each in a wrapping row, and a code span is
  an element with `.px_1()` and `.rounded_md()`. `app/md.rs` is where that lives.
- `gpui` on crates.io (0.2.2) is not the line the component library uses. Both
  frontends depend on `gpui-kit`, which pins `gpui-pre` and re-exports the
  framework, `gpui-base` and `gpui-component`. Adding plain `gpui` beside it will
  not resolve, and mixing them is not a version bump.
- **Icons need an asset source, and the component one is not the whole set.**
  `gpui_kit::init` registers none, so every `IconName` draws nothing until the
  application is built `.with_assets(...)`; and `gpui_kit::assets::Assets` (the
  component bundle) has no `X`, so a mark that has to draw wants `AllAssets`.
  A glyph that resolves to nothing looks exactly like a layout bug, and is not
  one.
- **`overflow_y_scroll` and `track_scroll` are on
  `StatefulInteractiveElement`**, so a scroll container needs an `.id(...)`
  before either of them or the method is not found on `Div` at all.
- **`use gpui_kit::*` shadows the builtin `#[test]`** with gpui's own where the
  test-support feature is on, and gpui's wants a context. A test module names the
  parent's items instead of globbing them.
- `h_flex` and `v_flex` are `gpui-base` extensions rather than GPUI itself:
  `use gpui_kit::*` does not bring them in, and `IconName` means two different
  enums depending on which layer it is imported from. The full catalog is
  `gpui_kit::assets::IconName`.
- **A child of a scrolling flex column shrinks unless it says not to.** A tall
  code block was silently squeezed to fit and its text fell out of the bottom of
  its own box, which reads as the text being clipped by a border. Anything in a
  scroller that must keep its height wants `.flex_shrink_0()`.
- **Hover paints the surface, never a band around it.** A `.hover()` on a wrapper
  div washes a full-width rectangle around a card that has its own face; the wash
  goes on the card (in `view`), and the wrapper keeps only the cursor and the
  click.
- **A hex token is written `#rrggbbaa`, never `aarrggbb`.** `app/theme.rs`'s
  `hex()` takes rgb and an alpha separately for that reason: reading a web hex as
  ARGB turns every alpha into the blue channel, and the whole window comes out
  lavender and nearly transparent. That bug happened once and it read as a palette
  mistake, not a parse mistake.
- Motion is `gpui_base`'s `animation::EffectTransition`, reached as
  `gpui_kit::component::animation::EffectTransition`. `app/motion.rs` is the one
  place that decides a duration, an easing or a curve; the pointer's blends run
  through `motion::mark_on_hover` on the surface itself, and the window's own
  33ms pump keeps it repainting while any blend or a run in flight is alive.
  A second call site with its own numbers is how a window ends up feeling like a
  demo reel.
- **Enter and the newline key.** `\r` is always `KeyCode::Enter`; `0x0A` in
  raw mode is `Char('j') + CONTROL`, not Enter. A newline key needs the terminal
  to report it, and which one it reports depends on the terminal, not on the OS:
  `CSI 13;2 u` (Enter+SHIFT) from a terminal with the kitty keyboard protocol, or
  `1b 0d` (escape, return, which is Enter+ALT) from a terminal that has the key
  mapped. Both are bound. crossterm parses the CSI u form whether or not the
  flags were pushed, and returns `Err` for xterm's older `CSI 27;M;K~` form, so
  an unparseable sequence is dropped rather than fatal.
  - what the terminal *claims* is not evidence. `supports_keyboard_enhancement()`
    answered true on that same terminal while shift-enter arrived as `1b 0d`. It
    was a gate on the start of a live session first (wrong: a terminal that
    answers no can still send the encodings) and a status-line warning second
    (wrong the other way: it was true and the key still failed). Both are gone.
    `fastrs --keys` prints the bytes, which is the only honest source, and asking
    it is step one before touching the binding.
  - an unparseable key sequence is dropped, not fatal. crossterm returns `Err`
    for xterm's older `CSI 27;M;K~` form, and `event::read()?` used to turn that
    into the app dying on a keypress.
  - Linux and Windows terminals mostly reach it through alt-enter, since most of
    them send escape for alt and only report a shifted enter from kitty (Windows
    Terminal from 1.25). ctrl-j is bound as the backup, since raw mode makes
    0x0A a key of its own and it is the only spelling that arrives in a terminal
    reporting neither: it was rejected as *the* key, not as a second one, and the
    panel lists it beside shift-enter so the one that always works is not the
    one nobody is told about.
- The app has been built and looked at. Five traps, each of which cost a build
  to learn:
  - **The projects system already exists in the worker; do not re-invent it.**
    `handler/session/store.py` holds `~/.cuacode/projects.json` (promoted dirs,
    pin order, per-dir names and icons) and `main.py` answers `project.list`,
    `project.add`, `project.rename`, `project.remove`, `project.pin`,
    `project.move` with a `projects` envelope. A project is a directory the user
    promoted, not one derived from a `cwd`; `scripts/lifecheck-projects.py` is
    its real-wire test, 22 asserts. The rail derives its three levels
    from that payload plus each session's `cwd` and adds nothing to the file.
  - **A permission request blocks the worker until the frontend answers it.**
    `main.py`'s `ask_permission` is an `ipc.call` with `timeout=None`, so a
    frontend that ignores the `permission` envelope does not fail loudly, it
    hangs. `agent.rs` answers it with
    `Session::reply(id, "permission", {"allow": bool})`.
  - **The window is opaque on purpose, and that is a decision, not an unfinished
    material.** `WindowBackgroundAppearance::Blurred` blurs the desktop, but on
    this platform it also makes every opaque token render see-through: the rail
    and the transcript showed the app behind them. gpui-pre has no per-element
    backdrop filter either, so a translucent composer would only ever show a
    sharp page through itself. Solid ember, checked against a contrast target in
    `theme.rs`, is the honest answer. The material the old GUI corrected at
    runtime is not corrected here because nothing translucent is drawn over it.
  - **Escape has to live on the root.** The field holds focus while a menu is
    open, so a key pressed into the field never reaches the menu: an escape
    handler on the menu itself would never fire, and a popover could only be
    dismissed by pressing its chip again.
  - Motion is `gpui_base`'s `animation::EffectTransition`, reached as
    `gpui_kit::component::animation::EffectTransition`. `app/motion.rs` is the one
    place that decides a duration, an easing or a curve; the pointer's blends run
    through `motion::mark_on_hover` on the surface itself, and the window's own
    33ms pump keeps it repainting while any blend or a run in flight is alive.
    A second call site with its own numbers is how a window ends up feeling like
    a demo reel.
- `rust/target/` is build output. Do not search it or read it.
- `app/` reads the state root directly and needs no worker to draw, so a bug in
  it is never the worker's. `CUACODE_HOME` points it at a scratch root, which is
  how its own tests and the empty-state pass run.

## What is not done

In the README, under "Not done". The short version: no syntax highlighting in
the conversation's code spans, the app's model chip is display-only (a picker it
has no list for would be pretending), its field is single-line on purpose (a
multi-line pill is a rebuild of the input, not a restyle), the sidebar's `Files`
tab is a placeholder, and neither frontend has been driven against a live model.
The app has no session rename or delete; mid-turn its send circle is the Stop,
and a press on it sends the same `cancel` a terminal's escape sends.
