// Command deck is a terminal frontend for the computer-use agent, built as an
// action tape rather than a chat log. Prose and thinking stream into the feed;
// each batch of tool calls is drawn as its own box, so what the agent did to
// the machine — in what order, and whether it worked — reads at a glance.
//
// main.go is the wiring. feed.go owns the block model and the feed rendering,
// calls.go decodes the tool payloads, view.go draws the frame.
//
//	ctrl+t  expand thinking         esc     stop the run in flight
//	tab     collapse tool calls     ctrl+b  background the running tool call
//	ctrl+c  quit
package main

import (
	"fmt"
	"os"
	"strings"
	"time"

	"cuacode/core/runner"
	"cuacode/core/session"

	tea "charm.land/bubbletea/v2"
)

// spinTickMsg drives the busy animation and, with it, the live clock and the
// per-call spinners in the open batch. ID invalidates ticks left over from a
// previous busy period.
type spinTickMsg struct{ ID int }

type model struct {
	sess   *session.Session
	status session.Snapshot // refreshed on every session event

	blocks  []*block // the feed, as structure
	wrapped []string // the feed, as rows at the current width
	scroll  int      // wrapped rows scrolled up from the bottom

	calls     *block // the batch of tool calls still collecting results, if any
	callCount int    // calls made in the current run
	callFail  int    // ...and how many of them came back an error
	sawOutput bool   // a result has landed, so the next prose starts a new batch

	input  []rune
	cursor int // rune index into input

	width, height int

	spinID int

	showThinking bool
	foldCalls    bool
	showArgs     bool // tool calls spell their arguments out instead of summarizing them

	insp          inspector         // the call being read in full, if any
	ov            overlay           // the menu on screen, if any
	permQueue     []permRequest     // worker questions waiting their turn
	permPolicy    map[string]string // standing answers, by tool name
	askMode       bool              // whether the worker asks before tool calls
	pickVision    bool              // the next providers reply opens the vision picker, not the provider one
	pickParams    bool              // ...or the params editor for the current model
	probing       bool              // the next providers reply opens nothing: the startup read, or the echo of a change just made
	modelProvider string            // whose models the open model picker is showing
	paramsModel   string            // ...and whose params the open params editor is editing
	provider      string            // who is answering, for the status bar
	modelID       string            // ...and on which model
	files         []option          // cached working-directory listing for @
	skills        []command         // user-invocable skills, as extra palette rows
	filesCut      bool              // ...and whether the walk stopped early
	effort        string            // the thinking level this conversation is set to
	effortSends   map[string]string // ...and what each rung of the ladder sends this model
	effortOff     bool              // ...and whether its bottom rung can be honoured at all
	ultra         bool              // ultracode: on only by typing its name
	ticking       bool              // the animation clock is running
	loading       bool              // a session load is in flight, so its reply replays
	needsClear    bool              // the feed just emptied; wipe the screen with it
	quitting      bool

	ctxMax    int // largest context reading seen, as the gauge's denominator
	running   bool
	runStart  time.Time
	lastRun   time.Duration
	animStart time.Time // fixed origin for every animation clock
}

func initialModel() *model {
	m := &model{
		status:     session.Snapshot{State: session.Idle},
		animStart:  time.Now(),
		permPolicy: map[string]string{},
		// Believed until a listing says otherwise: a rung greyed out on a
		// guess is worse than one that turns out to have been refusable.
		effortOff: true,
		askMode:   true, // asking is the default; /permissions turns it off
	}
	m.push(&block{kind: kHint})
	return m
}

// contentHeight is the number of rows the feed gets. It comes from the same
// division render uses, so scrolling and drawing can never disagree about how
// much of the feed is on screen.
func (m *model) contentHeight() int {
	h, _, _ := m.layout()
	return h
}

// viewRows is what the content area is showing: the feed, or the call being
// read in full. Scrolling, measuring and drawing all go through it, so the
// three can never disagree about which of the two is on screen.
func (m *model) viewRows() []string {
	if m.insp.on {
		return m.inspectRows()
	}
	return m.wrapped
}

// viewScroll is the scroll position belonging to whatever viewRows is showing.
// They are kept apart: closing the inspector should put the feed back exactly
// where it was.
func (m *model) viewScroll() *int {
	if m.insp.on {
		return &m.insp.scroll
	}
	return &m.scroll
}

func (m *model) maxScroll() int {
	return max(len(m.viewRows())-m.contentHeight(), 0)
}

func (m *model) cacheFlags() uint8 {
	var f uint8
	if m.showThinking {
		f |= flagThinking
	}
	if m.foldCalls {
		f |= flagCalls
	}
	if m.ultra {
		f |= flagUltra
	}
	if m.showArgs {
		f |= flagArgs
	}
	return f
}

// runtime is the elapsed time of the current run, or of the last one once it
// has finished.
func (m *model) runtime() time.Duration {
	if m.running {
		return time.Since(m.runStart)
	}
	return m.lastRun
}

// rebuild re-renders the feed from the blocks. Blocks cache their rows, so a
// streamed token only re-wraps the block it landed in; the rest is a copy.
//
// The viewport sticks to the bottom unless the user has scrolled away from it,
// in which case it holds its distance from the end as rows arrive.
func (m *model) rebuild() {
	before := len(m.wrapped)
	flags := m.cacheFlags()

	rows := make([]string, 0, before+8)
	for i, b := range m.blocks {
		if i > 0 {
			rows = append(rows, "")
		}
		rows = append(rows, m.rowsFor(b, flags)...)
	}
	m.wrapped = rows

	if m.scroll < 3 {
		m.scroll = 0
		return
	}
	m.scroll = min(max(m.scroll+len(m.wrapped)-before, 0), m.maxScroll())
}

// wheelRows is how far one notch of the wheel moves the feed. One row: a
// trackpad sends notches fast enough that anything more overshoots.
const wheelRows = 1

func (m *model) scrollBy(n int) {
	at := m.viewScroll()
	*at = min(max(*at+n, 0), m.maxScroll())
}

// frameRate is the repaint interval while something is animating. It is fast
// enough that a travelling highlight glides rather than steps, and it costs
// almost nothing: only blocks that animate re-render, and the rest of the feed
// is copied from the rows it was already holding.
const frameRate = 50 * time.Millisecond

func startSpin(id int) tea.Cmd {
	return tea.Tick(frameRate, func(time.Time) tea.Msg {
		return spinTickMsg{ID: id}
	})
}

// anim is the animation clock: monotonic, continuous, and independent of how
// often the screen actually repaints.
func (m *model) anim() time.Duration { return time.Since(m.animStart) }

// wantTick reports whether anything on screen is moving. The clock is only kept
// running while something is: an idle screen should cost nothing at all.
func (m *model) wantTick() bool {
	return session.Busy(m.status.State) || m.ov.kind == ovEffort || m.ultra || m.inspectWaiting()
}

// ensureTick starts the clock if something has just begun moving and it was not
// already running. Starting a second one would double the frame rate.
func (m *model) ensureTick() tea.Cmd {
	if m.ticking || !m.wantTick() {
		return nil
	}
	m.ticking = true
	m.spinID++
	return startSpin(m.spinID)
}

// spinGlyph steps at its own pace rather than once per repaint, so raising the
// frame rate does not make the spinner spin faster.
func (m *model) spinGlyph() string {
	return spinFrames[int(m.anim()/(130*time.Millisecond))%len(spinFrames)]
}

// withClear pairs a command with a full repaint when the feed has just been
// emptied. Every frame is drawn at the full height, so in principle nothing can
// survive one; in practice a shrinking feed has been seen to leave rows from
// the frame before it behind. Forcing the screen clean costs a single repaint
// and takes the question away.
func (m *model) withClear(cmd tea.Cmd) tea.Cmd {
	if !m.needsClear {
		return cmd
	}
	m.needsClear = false
	if cmd == nil {
		return tea.ClearScreen
	}
	return tea.Batch(cmd, tea.ClearScreen)
}

func (m *model) quit() (tea.Model, tea.Cmd) {
	if m.sess != nil {
		m.sess.Close()
	}
	return m, tea.Quit
}

// send puts a message on the wire and echoes it into the feed.
//
// Typed while a run is going it is a mid-turn message, not the next turn: the
// worker holds it back and speaks it into the round already in flight, after
// the tool results, which is the only place a user message is legal for every
// provider. Everything below the echo is skipped in that case — the run it
// would start is the one already running, and resetting its clock or reopening
// its call group would report the wrong thing about it.
func (m *model) send(text string) tea.Cmd {
	m.sess.SendChat(text)
	m.status = m.sess.Snapshot()

	if m.running {
		m.push(&block{kind: kUser, text: text})
		m.rebuild()
		return nil
	}

	m.closeCalls()
	m.callCount, m.callFail = 0, 0
	m.push(&block{kind: kUser, text: text})
	m.rebuild()

	m.running, m.runStart = true, time.Now()
	m.ticking = true
	m.spinID++
	return startSpin(m.spinID)
}

func (m *model) Init() tea.Cmd { return nil }

func (m *model) handleKey(msg tea.KeyPressMsg) (tea.Model, tea.Cmd) {
	ctrl := msg.Mod&tea.ModCtrl != 0
	alt := msg.Mod&tea.ModAlt != 0

	// Ctrl+C always quits, even with a prompt up. Everything else goes to the
	// menu while one is open.
	if msg.Code == 'c' && ctrl {
		return m.quit()
	}
	if m.overlayActive() {
		m.handleOverlayKey(msg, ctrl, alt)
		if m.quitting {
			return m.quit()
		}
		m.rebuild()
		return m, m.withClear(m.ensureTick())
	}
	// The inspector takes the keyboard while it is up, for the same reason a
	// menu does: it is a page you are reading, not a field you are typing in.
	if m.insp.on {
		m.handleInspectKey(msg, ctrl)
		return m, m.withClear(m.ensureTick())
	}

	switch {
	case msg.Code == '/' && len(m.input) == 0:
		m.insert('/')
		m.openCommands(0)

	case msg.Code == '@':
		// The anchor is the trigger itself: choosing a file replaces the "@"
		// and whatever was typed after it, rather than appending to it.
		m.insert('@')
		m.openFiles(m.cursor - 1)

	case msg.Code == tea.KeyEsc:
		if m.sess != nil && session.Busy(m.status.State) {
			m.sess.Cancel()
		}

	// Only while a tool is actually running. The worker discards a press that
	// lands anywhere else, and offering it during a stream would just look
	// broken.
	case msg.Code == 'b' && ctrl:
		if m.sess != nil && m.status.State == session.Tools {
			m.sess.Background()
		}

	case msg.Code == 't' && ctrl:
		m.showThinking = !m.showThinking
		m.rebuild()

	// Shift+Tab is the opposite of Tab, and reads as it: one takes the batch
	// down to a line, the other opens every call in it out to its arguments.
	// It arrives as \x1b[Z, which every mainstream terminal sends.
	case msg.Code == tea.KeyTab && msg.Mod&tea.ModShift != 0:
		m.showArgs = !m.showArgs
		m.rebuild()

	case msg.Code == tea.KeyTab:
		m.foldCalls = !m.foldCalls
		m.rebuild()

	// The row says what a call was; this says what it sent and what came back.
	// Offered while a call is still running too — that is when "what is it
	// actually doing" gets asked.
	case msg.Code == 'o' && ctrl:
		m.openInspector()
		m.rebuild()

	case msg.Code == tea.KeyEnter && (msg.Mod&tea.ModShift != 0 || alt):
		// Shift+Enter needs a terminal that reports modifiers on Enter at all
		// (Kitty, Ghostty, WezTerm, iTerm2 with the setting on); Alt+Enter is
		// the fallback for the ones that do not, Terminal.app among them.
		m.insert('\n')

	case msg.Code == tea.KeyEnter:
		text := strings.TrimSpace(string(m.input))
		if text == "" {
			break
		}
		m.input, m.cursor = m.input[:0], 0

		// The word is the switch. It is never sent anywhere — typing it is the
		// whole interface, and typing it again puts things back.
		if strings.EqualFold(text, ultracodeWord) {
			m.toggleUltra()
			m.rebuild()
			return m, m.ensureTick()
		}
		return m, m.send(text)

	case msg.Code == tea.KeySpace:
		m.insert(' ')

	case msg.Code == tea.KeyBackspace:
		if m.cursor > 0 {
			m.input = append(m.input[:m.cursor-1], m.input[m.cursor:]...)
			m.cursor--
		}

	case msg.Code == tea.KeyDelete:
		if m.cursor < len(m.input) {
			m.input = append(m.input[:m.cursor], m.input[m.cursor+1:]...)
		}

	case msg.Code == 'u' && ctrl:
		m.input, m.cursor = m.input[m.cursor:], 0

	case msg.Code == 'w' && ctrl:
		start := m.wordStart()
		m.input = append(m.input[:start], m.input[m.cursor:]...)
		m.cursor = start

	case msg.Code == tea.KeyLeft:
		m.cursor = max(m.cursor-1, 0)

	case msg.Code == tea.KeyRight:
		m.cursor = min(m.cursor+1, len(m.input))

	case msg.Code == tea.KeyHome:
		m.cursor = m.lineStart()

	case msg.Code == tea.KeyEnd:
		m.cursor = m.lineEnd()

	// With a multi-line message being typed, the arrows belong to the cursor;
	// with a single line there is nowhere for them to go, so they scroll.
	case msg.Code == tea.KeyUp:
		if !m.moveLine(-1) {
			m.scrollBy(1)
		}

	case msg.Code == tea.KeyDown:
		if !m.moveLine(1) {
			m.scrollBy(-1)
		}

	case msg.Code == tea.KeyPgUp:
		m.scrollBy(m.contentHeight())

	case msg.Code == tea.KeyPgDown:
		m.scrollBy(-m.contentHeight())

	default:
		if msg.Text != "" && !ctrl && !alt {
			m.insert([]rune(msg.Text)...)
		}
	}
	return m, nil
}

// handleInspectKey drives the open call: up and down through it, left and right
// between calls, escape back to the feed. Everything else is swallowed rather
// than typed — the input row is not on screen to receive it.
func (m *model) handleInspectKey(msg tea.KeyPressMsg, ctrl bool) {
	switch {
	case msg.Code == tea.KeyEsc, msg.Code == 'o' && ctrl, msg.Code == 'q' && !ctrl:
		m.closeInspector()

	case msg.Code == tea.KeyLeft:
		m.stepInspector(-1)
	case msg.Code == tea.KeyRight:
		m.stepInspector(1)

	// The inspector reads top-down, so its arrows do too — down goes further
	// into the page, which is the opposite of the feed's arrows walking back up
	// a conversation.
	case msg.Code == tea.KeyUp:
		m.scrollBy(-1)
	case msg.Code == tea.KeyDown:
		m.scrollBy(1)
	case msg.Code == tea.KeyPgUp:
		m.scrollBy(-m.contentHeight())
	case msg.Code == tea.KeyPgDown:
		m.scrollBy(m.contentHeight())
	case msg.Code == tea.KeyHome:
		m.insp.scroll = 0
	case msg.Code == tea.KeyEnd:
		m.insp.scroll = m.maxScroll()
	}
}

// flattenPaste folds every run of whitespace in pasted text down to a single
// space and drops control characters. Spacing at the edges is kept — a paste
// dropped between two words has to stay between them — but nothing that would
// submit the line early, or move the cursor, survives.
func flattenPaste(s string) string {
	var b strings.Builder
	b.Grow(len(s))

	pending := false
	for _, r := range s {
		switch {
		case r == ' ' || r == '\t' || r == '\n' || r == '\r':
			pending = true
		case r < 0x20 || r == 0x7f:
			// control characters never reach the buffer
		default:
			if pending {
				b.WriteByte(' ')
				pending = false
			}
			b.WriteRune(r)
		}
	}
	if pending {
		b.WriteByte(' ')
	}
	return b.String()
}

func (m *model) insert(rs ...rune) {
	m.input = append(m.input[:m.cursor], append(rs, m.input[m.cursor:]...)...)
	m.cursor += len(rs)
}

// lineStart and lineEnd bound the input line the cursor is on, which is the
// whole buffer until the message has newlines in it.
func (m *model) lineStart() int {
	i := m.cursor
	for i > 0 && m.input[i-1] != '\n' {
		i--
	}
	return i
}

func (m *model) lineEnd() int {
	i := m.cursor
	for i < len(m.input) && m.input[i] != '\n' {
		i++
	}
	return i
}

// moveLine steps the cursor one input line up or down, keeping its column
// where it can. It reports false when there is no such line, leaving the key
// to whatever else wants it.
func (m *model) moveLine(dir int) bool {
	lines, row, col := m.inputLines()
	target := row + dir
	if target < 0 || target >= len(lines) {
		return false
	}

	// Count back to the start of the target line, then step in by the column.
	at := 0
	for i := 0; i < target; i++ {
		at += len(lines[i])
		// A line ended by an inserted newline costs a rune; one ended by
		// wrapping does not.
		if at < len(m.input) && m.input[at] == '\n' {
			at++
		}
	}
	m.cursor = min(at+col, at+len(lines[target]))
	return true
}

// wordStart is the index one ctrl+w deletion would leave the cursor at.
func (m *model) wordStart() int {
	i := m.cursor - 1
	for i >= 0 && m.input[i] == ' ' {
		i--
	}
	for i >= 0 && m.input[i] != ' ' {
		i--
	}
	return i + 1
}

// handleSessionEvent folds one worker event into the model. Parsing and state
// bookkeeping already happened in core/session; this only decides what to show
// and whether the spinner runs.
func (m *model) handleSessionEvent(ev session.Event) tea.Cmd {
	if ev.ParseErr == nil {
		m.status = ev.Snapshot
		// The window as the worker states it, when it knows the model's size.
		// When it does not, the largest reading ever taken stands in for full —
		// it is the one from when the least had been consumed — which is the
		// only denominator available to a worker that reports what is left and
		// nothing else.
		if m.status.ContextMax > 0 {
			m.ctxMax = m.status.ContextMax
		} else {
			m.ctxMax = max(m.ctxMax, m.status.ContextLeft)
		}

		// Envelopes addressed to the frontend rather than to the feed: a
		// question the worker is blocked on, or the answer to something a menu
		// asked for.
		// The worker only asks frontends that have said they answer, so the
		// mode is (re)declared every time it comes up.
		if s := ev.Parsed.State; s == "ready" || s == "startup" {
			if m.askMode {
				m.command("permission.mode", map[string]any{"mode": "ask"})
			}
			// Who is answering is on the status bar from the first frame, not
			// from the first time a picker is opened. The reply to this one is
			// read and dropped rather than shown.
			if m.provider == "" {
				m.probing = true
				m.command("provider.list", nil)
			}
			// Skills are files on disk and one may have been written since the
			// last read, so the palette asks again whenever the worker comes
			// up rather than only once.
			m.command("skill.list", nil)
		}

		switch ev.Parsed.Type {
		case "permission":
			m.askPermission(ev.Parsed.ID, ev.Parsed.Data)
			m.rebuild()
			return nil
		case "sessions":
			m.openSessions(ev.Parsed.Data)
			m.rebuild()
			return nil
		case "skills":
			// Nothing opens: the rows join the slash palette for the next time
			// it is opened.
			m.takeSkills(ev.Parsed.Data)
			return nil

		case "providers":
			m.openProviders(ev.Parsed.Data)

		case "models":
			m.openModels(ev.Parsed.Data)
			m.rebuild()
			return nil

		case "context":
			m.takeContext(ev.Parsed.Data)
			m.rebuild()
			return m.ensureTick()

		case "usage":
			m.takeUsage(ev.Parsed.Data)
			m.rebuild()
			return m.ensureTick()

		case "detail":
			// One call, read back out of the records. It belongs to the
			// inspector rather than the feed, which never asked for it.
			m.takeDetail(ev.Parsed.Data)
			return nil
		}
	}
	m.fold(ev)
	m.rebuild()
	m.refreshInspector()

	if !ev.StateChanged {
		return nil
	}
	m.spinID++ // invalidates any tick still in flight
	if session.Busy(m.status.State) {
		return startSpin(m.spinID)
	}
	m.finish()
	return nil
}

func (m *model) Update(msg tea.Msg) (tea.Model, tea.Cmd) {
	switch msg := msg.(type) {
	case spinTickMsg:
		if msg.ID == m.spinID {
			if !m.wantTick() {
				m.ticking = false
				return m, nil
			}
			m.rebuild() // only what is actually moving re-renders
			return m, startSpin(m.spinID)
		}

	case tea.WindowSizeMsg:
		m.width, m.height = msg.Width, msg.Height
		m.rebuild()

	case tea.InterruptMsg:
		return m.quit()

	case tea.MouseWheelMsg:
		// A permission prompt owns the wheel while it is up: the block it is
		// asking about is the only thing on screen worth scrolling, and the
		// feed behind it is not going anywhere.
		if m.ov.kind == ovPermission {
			switch msg.Button {
			case tea.MouseWheelUp:
				m.scrollPermBody(-wheelRows)
			case tea.MouseWheelDown:
				m.scrollPermBody(wheelRows)
			}
			return m, nil
		}

		// Up is always back towards the start of what is on screen, which is a
		// different direction in the two of them: the feed is anchored to its
		// end and the inspector to its beginning.
		up := wheelRows
		if m.insp.on {
			up = -wheelRows
		}
		switch msg.Button {
		case tea.MouseWheelUp:
			m.scrollBy(up)
		case tea.MouseWheelDown:
			m.scrollBy(-up)
		}

	case tea.PasteMsg:
		// Bracketed paste arrives whole. Newlines would submit a line at a
		// time, so they fold into spaces and the paste stays one message.
		m.insert([]rune(flattenPaste(msg.Content))...)

	case tea.KeyPressMsg:
		return m.handleKey(msg)

	case session.Event:
		cmd := m.handleSessionEvent(msg)
		if cmd == nil {
			cmd = m.ensureTick()
		}
		return m, m.withClear(cmd)
	}
	return m, nil
}

func (m *model) View() tea.View {
	v := tea.NewView(m.render())
	v.AltScreen = true
	// The wheel has to be ours. Left to the terminal it scrolls the terminal's
	// own buffer instead of the feed, and a terminal that keeps scrollback for
	// the alternate screen — iTerm2 does, by default — has a buffer full of the
	// rows earlier frames pushed off the top, so the wheel walks back through
	// the wreckage of old frames rather than through the conversation.
	//
	// The cost is drag-to-copy, which now needs the terminal's bypass modifier:
	// Option in iTerm2 and Ghostty, Shift almost everywhere else.
	v.MouseMode = tea.MouseModeCellMotion
	return v
}

const usage = `deck — terminal frontend for the cuacode agent

usage:
  deck                 start a new conversation
  deck --resume        pick an earlier conversation to carry on
  deck --resume <id>   carry on that conversation

  ./run.sh deck --resume   the same, straight from source
`

// resumeFlag reads the command line. It returns whether --resume was given and,
// if it named one, which conversation.
func resumeFlag(args []string) (resume bool, id string, help bool) {
	for i := 0; i < len(args); i++ {
		switch arg := args[i]; {
		case arg == "-h", arg == "--help":
			return false, "", true

		case arg == "--resume", arg == "-r":
			resume = true
			// A bare --resume means "show me the list"; an id after it means
			// that one. A following flag is not an id.
			if i+1 < len(args) && !strings.HasPrefix(args[i+1], "-") {
				i++
				id = args[i]
			}

		case strings.HasPrefix(arg, "--resume="):
			resume, id = true, strings.TrimPrefix(arg, "--resume=")
		}
	}
	return resume, id, false
}

func main() {
	resume, resumeID, help := resumeFlag(os.Args[1:])
	if help {
		fmt.Print(usage)
		return
	}

	m := initialModel()
	p := tea.NewProgram(m)

	sess, err := runner.Start(func(ev session.Event) { p.Send(ev) })
	if err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
	m.sess = sess
	defer sess.Close()

	// Declared here rather than on the worker's first "ready" line: that line
	// can arrive before this assignment, and the worker only asks frontends
	// that have said they answer. Missing it means never being asked at all.
	if m.askMode {
		sess.Command("permission.mode", map[string]any{"mode": "ask"})
	}

	// Asked for before the loop starts, so the picker is already up — or the
	// conversation already replaying — by the time the first frame is drawn.
	switch {
	case resume && resumeID != "":
		m.loading = true
		sess.Command("session.load", map[string]any{"id": resumeID})
	case resume:
		sess.Command("session.list", nil)
	}

	if _, err := p.Run(); err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
}
