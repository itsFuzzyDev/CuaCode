package main

// One widget serves all three menus: the permission prompt, the slash-command
// palette, and the @file picker. They differ only in what fills the list and
// what happens on Enter, so they share the filtering, the selection, the key
// handling and the drawing — and there is only one place where a menu can get
// its geometry wrong.

import (
	"sort"
	"strings"

	tea "charm.land/bubbletea/v2"
)

type overlayKind int

const (
	ovNone overlayKind = iota
	ovPermission
	ovCommands
	ovFiles
	ovSessions
	ovProviders
	ovModels
	ovVision
	ovEffort
)

// option is one row of a menu.
type option struct {
	label string // what the row says, and what its width is measured from
	raw   string // pre-styled label, when a plain colour will not do
	hint  string // dimmed, right-aligned
	value string // what choosing it means, for whoever handles the choice
	tone  string // label colour; empty for the default
}

// permRequest is a worker question waiting on an answer.
type permRequest struct {
	id      string
	name    string
	args    string         // formatted, for the line above the choices
	full    map[string]any // ...and the arguments themselves, which is what is actually being allowed
	summary string         // what the tool says the call would do
	diff    string         // ...and the patch, for a write or an edit
	content string         // ...or the whole file, when it is being created
	lang    string         // which highlighter the two of those are drawn with
	key     string         // what a standing allow is filed under
	scope   string         // how that standing allow reads on the prompt
}

type overlay struct {
	kind  overlayKind
	title string
	note  string // a line of context above the list

	all   []option // everything the menu could offer
	shown []int    // indices of all[] matching the filter
	sel   int      // index into shown

	filter string // typed after the trigger character
	anchor int    // rune index in the input where the trigger sits

	perm     permRequest
	expanded bool // the permission prompt is using every row the screen can spare
	bodyAt   int  // first row of the call's body on screen, for scrolling it
	bodyMax  int  // ...and how far it can be scrolled, as of the last draw

	// The body, rendered. Cached because it is rebuilt on every frame otherwise
	// — twice, once to measure and once to draw — and parsing a thousand-line
	// patch twenty times a second to show ten of its rows is work nobody asked
	// for.
	body  []string
	bodyW int
}

// maxRows caps how tall a menu gets, so a long list never eats the feed.
const maxRows = 8

func (m *model) overlayActive() bool { return m.ov.kind != ovNone }

// openOverlay puts a menu up, filtered from the start so a trigger typed with
// text after it lands on the right row immediately.
func (m *model) openOverlay(kind overlayKind, title string, opts []option, anchor int) {
	m.ov = overlay{kind: kind, title: title, all: opts, anchor: anchor}
	m.ov.refilter()
}

func (m *model) closeOverlay() { m.ov = overlay{} }

// refilter recomputes the visible rows. Matching is subsequence-based, so "sn"
// finds "session.new" — the usual thing a palette does.
func (o *overlay) refilter() {
	needle := strings.ToLower(o.filter)
	o.shown = o.shown[:0]

	type scored struct{ idx, rank int }
	var hits []scored

	for i, opt := range o.all {
		hay := strings.ToLower(opt.label)
		switch {
		case needle == "":
			hits = append(hits, scored{i, 0})
		case strings.HasPrefix(hay, needle):
			hits = append(hits, scored{i, 0})
		case strings.Contains(hay, needle):
			hits = append(hits, scored{i, 1})
		case subsequence(hay, needle):
			hits = append(hits, scored{i, 2})
		}
	}

	// Stable by rank, then by original order: the list must not reshuffle
	// under the cursor as characters arrive.
	sort.SliceStable(hits, func(a, b int) bool { return hits[a].rank < hits[b].rank })
	for _, h := range hits {
		o.shown = append(o.shown, h.idx)
	}
	o.sel = min(o.sel, max(len(o.shown)-1, 0))
}

func subsequence(hay, needle string) bool {
	i := 0
	for _, r := range hay {
		if i < len(needle) && rune(needle[i]) == r {
			i++
		}
	}
	return i == len(needle)
}

func (o *overlay) selected() (option, bool) {
	if o.sel < 0 || o.sel >= len(o.shown) {
		return option{}, false
	}
	return o.all[o.shown[o.sel]], true
}

func (o *overlay) move(n int) {
	if len(o.shown) == 0 {
		return
	}
	o.sel = (o.sel + n + len(o.shown)) % len(o.shown)
}

// ---------------------------------------------------------------------------
// drawing

// overlayHeight is how many rows the menu will occupy, so the feed can be
// shortened by exactly that much and nothing overlaps. It is the length of what
// renderOverlay actually produces rather than a second calculation of it: the
// two agreeing is what keeps the frame the right height, and a menu that draws
// itself differently — the effort meter — cannot get that wrong here.
func (m *model) overlayHeight() int { return len(m.renderOverlay()) }

// renderOverlay draws the menu directly above the input row. It returns exactly
// overlayHeight rows, because the feed was already shortened by that many.
func (m *model) renderOverlay() []string {
	if !m.overlayActive() {
		return nil
	}
	o := &m.ov
	width := m.bodyW()

	// A question needing an answer is marked in the colour of the thing it is
	// about to run; a picker is just chrome.
	lead := cRule
	if o.kind == ovPermission {
		lead = cCall
	}

	title := margin + paint(lead, "▸ ") + paint(cInk+bold, o.title)
	if o.filter != "" {
		title += paint(cFaint, "  ") + paint(cCall, o.filter)
	}
	if hint := o.hint(); hint != "" {
		if gap := 2 + width - vw(title) - vw(hint); gap > 1 {
			title += strings.Repeat(" ", gap) + paint(cGhost, hint)
		}
	}

	// One menu draws itself: the effort meter is a shape, not a list.
	if o.kind == ovEffort {
		return append(append([]string{title}, m.renderEffort()...), "")
	}

	rows := []string{title}
	switch {
	case o.kind == ovPermission && (len(o.perm.full) > 0 || o.perm.summary != ""):
		// What is actually being allowed. Never a summary of the arguments and
		// never truncated to a line: agreeing to a call you have seen the first
		// eighty characters of is agreeing to something you have not read.
		rows = append(rows, m.permBodyRows(o, width)...)
		rows = append(rows, "")

	case o.note != "":
		// Indented past the title and set in the code tone: on a permission
		// prompt this is the literal command about to run, and it is the one
		// thing worth reading twice.
		rows = append(rows, margin+"    "+paint(sCode, trunc(o.note, width-4)), "")
	}

	if len(o.shown) == 0 {
		return append(rows, margin+"    "+paint(cGhost, "nothing matches"), "")
	}

	// Keep the selection in view without moving it around inside the window.
	start := 0
	if n := min(len(o.shown), maxRows); o.sel >= n {
		start = o.sel - n + 1
	}

	for i := start; i < len(o.shown) && i-start < maxRows; i++ {
		opt := o.all[o.shown[i]]
		tone, mark := cMuted, "  "
		if opt.tone != "" {
			tone = opt.tone
		}
		if i == o.sel {
			tone, mark = cInk+bold, paint(cCall, "› ")
		}

		// Options hang under the title rather than sharing its column, so the
		// menu reads as one thing instead of a stack of unrelated rows.
		body := paint(tone, trunc(opt.label, width-6))
		if opt.raw != "" && vw(opt.label) <= width-6 {
			// A row that styles itself: its width still comes from label, so
			// the layout cannot depend on what the colours are doing.
			body = opt.raw
		}
		row := margin + "  " + mark + body
		if opt.hint != "" {
			if gap := 2 + width - vw(row) - vw(opt.hint); gap > 1 {
				row += strings.Repeat(" ", gap) + paint(cGhost, opt.hint)
			}
		}
		rows = append(rows, row)
	}
	return append(rows, "")
}

// permBodyRows draws what the call would do, above the choices.
//
// Three shapes, in order of how much they tell you. A write or an edit has a
// patch, and a patch is what gets drawn — line numbers, tinted changes, the
// code coloured — because that is the thing being approved and a paragraph of
// grey text is not it. A file being created has no patch but has its content,
// which is the same picture with every line arriving. Everything else gets its
// arguments in the key column the inspector uses.
//
// The prompt shares the screen with the choices it is asking you to make, and
// those must never be pushed off it, so the block is bounded by what is left
// after everything else has its rows. What does not fit says so and can be
// opened with ctrl+o, which is the same key that opens a call in the feed.
func (m *model) permBodyRows(o *overlay, width int) []string {
	p := &o.perm

	// The summary is one line and it says which file and what kind of change,
	// so it stays put while the block under it scrolls. Losing "edit main.go"
	// off the top on the way to line 200 of the patch is losing the sentence
	// the patch is an answer to.
	var head []string
	if p.summary != "" {
		head = append(head, margin+"  "+paint(cMuted, trunc(plain(p.summary), width-2)))
	}

	rows := m.permBody(o, width)

	// Everything on screen that is not the block: the title, the blank under
	// it, three choices, the trailing blank, the input row and the two the
	// frame always takes — and the summary, which is counted separately
	// because a call without one does not have it.
	const chrome = 9
	budget := max(m.height-chrome-len(head), 1)
	if !o.expanded {
		budget = min(budget, permBodyRowsShown)
	}
	if len(rows) <= budget {
		o.bodyAt, o.bodyMax = 0, 0
		return append(head, rows...)
	}

	// One row of the budget goes to the footer, which says where in the block
	// the window is: a view of ten lines out of four hundred that does not say
	// so is indistinguishable from the whole thing.
	window := max(budget-1, 1)
	o.bodyMax = len(rows) - window
	o.bodyAt = min(max(o.bodyAt, 0), o.bodyMax)

	out := append(head, rows[o.bodyAt:o.bodyAt+window]...)
	return append(out, margin+"  "+paint(cGhost, o.bodyFoot(window, len(rows))))
}

// permBody renders what the call would do, without deciding how much of it
// fits. Cached against the width it was drawn at.
func (m *model) permBody(o *overlay, width int) []string {
	if o.body != nil && o.bodyW == width {
		return o.body
	}
	p := &o.perm

	var rows []string
	switch {
	case p.diff != "":
		rows = diffRows(p.diff, p.lang, width)
	case p.content != "":
		rows = contentRows(p.content, p.lang, width)
	case len(p.full) > 0 && p.summary == "":
		rows = valueRows(p.full, width, 0, p.lang)
	}
	if rows == nil {
		rows = []string{}
	}
	o.body, o.bodyW = rows, width
	return rows
}

// bodyFoot is the line under a block too long to show whole: where you are in
// it, and the keys that move you.
func (o *overlay) bodyFoot(window, total int) string {
	pos := itoa(o.bodyAt+1) + "–" + itoa(o.bodyAt+window) + " of " + itoa(total)
	size := "ctrl+o taller"
	if o.expanded {
		size = "ctrl+o smaller"
	}
	return pos + sep + "shift+↑↓ scroll" + sep + size
}

// scrollBody moves the window over the call's body. The choices keep the plain
// arrows — the prompt is a question first — so the block gets the modified
// ones, and the page keys, which nothing else on a permission prompt wants.
func (o *overlay) scrollBody(n int) {
	o.bodyAt = min(max(o.bodyAt+n, 0), o.bodyMax)
}

// scrollPermBody is the same thing from a keypress, which can arrive before the
// prompt has ever been drawn. How far there is to scroll is a drawing decision,
// so the drawing is asked for first — it is cached, so asking costs nothing.
func (m *model) scrollPermBody(n int) {
	m.permBodyRows(&m.ov, m.bodyW())
	m.ov.scrollBody(n)
}

// permBodyRowsShown is how much of a call the prompt shows before the rest of
// it has to be scrolled to. Enough for a small hunk or every argument of an
// ordinary call, short enough that the choices stay where the eye expects them.
const permBodyRowsShown = 10

// hint is the right-hand key legend, which differs per menu because the
// permission prompt is the only one you cannot simply walk away from.
func (o *overlay) hint() string {
	if o.kind == ovPermission {
		return "↑↓ choose  ·  enter answer"
	}
	return "↑↓ choose  ·  enter select  ·  esc cancel"
}

// ---------------------------------------------------------------------------
// keys

// handleOverlayKey routes a keypress to the open menu. The permission prompt
// takes no filter text and cannot be dismissed: the worker is blocked on it, so
// every way out of it is an answer.
func (m *model) handleOverlayKey(msg tea.KeyPressMsg, ctrl, alt bool) {
	if m.ov.kind == ovPermission {
		// Reading the call is not answering it, so every key that only moves
		// the block is handled ahead of the ones that resolve the prompt:
		// nothing here may say yes by accident.
		if msg.Code == 'o' && ctrl {
			m.ov.expanded = !m.ov.expanded
			return
		}
		// Shift on the arrows, because the bare ones belong to the choices.
		// Alt does the same thing, for the terminals that report one modifier
		// on an arrow key and not the other.
		if shift := msg.Mod&tea.ModShift != 0; shift || alt {
			switch msg.Code {
			case tea.KeyUp:
				m.scrollPermBody(-1)
				return
			case tea.KeyDown:
				m.scrollPermBody(1)
				return
			}
		}
		switch msg.Code {
		case tea.KeyPgUp:
			m.scrollPermBody(-permBodyRowsShown)
			return
		case tea.KeyPgDown:
			m.scrollPermBody(permBodyRowsShown)
			return
		case tea.KeyHome:
			m.scrollPermBody(-1 << 30)
			return
		case tea.KeyEnd:
			m.scrollPermBody(1 << 30)
			return
		}
		switch msg.Code {
		case tea.KeyUp:
			m.ov.move(-1)
		case tea.KeyDown:
			m.ov.move(1)
		case tea.KeyEnter:
			if opt, ok := m.ov.selected(); ok {
				m.resolvePermission(opt.value)
			}
		case tea.KeyEsc:
			m.resolvePermission(permDenyOnce)
		}
		return
	}

	switch {
	case msg.Code == tea.KeyEsc:
		m.dismissMenu()

	case msg.Code == tea.KeyUp:
		m.ov.move(-1)

	case msg.Code == tea.KeyDown:
		m.ov.move(1)

	case msg.Code == tea.KeyEnter, msg.Code == tea.KeyTab:
		m.chooseMenu()

	case msg.Code == tea.KeyBackspace:
		// Backspacing over the trigger closes the menu, which is the only way
		// the filter and the input can disagree about what was typed.
		if m.cursor > 0 {
			m.input = append(m.input[:m.cursor-1], m.input[m.cursor:]...)
			m.cursor--
		}
		if m.ov.filter == "" {
			m.closeOverlay()
			return
		}
		m.ov.filter = m.ov.filter[:len(m.ov.filter)-1]
		m.ov.refilter()

	default:
		if msg.Text != "" && !ctrl && !alt {
			m.insert([]rune(msg.Text)...)
			m.ov.filter += msg.Text
			m.ov.refilter()
		}
	}
}

// dismissMenu closes a menu without acting, leaving what was typed in place.
func (m *model) dismissMenu() {
	kind, anchor := m.ov.kind, m.ov.anchor
	m.closeOverlay()

	// A picker opened by a trigger character takes the trigger with it, so an
	// abandoned "@" does not sit in the message.
	if (kind == ovFiles || kind == ovCommands) && anchor >= 0 && anchor < len(m.input) {
		m.input = append(m.input[:anchor], m.input[m.cursor:]...)
		m.cursor = anchor
	}
}

// chooseMenu acts on the highlighted row.
func (m *model) chooseMenu() {
	opt, ok := m.ov.selected()
	if !ok {
		return
	}
	kind := m.ov.kind

	switch kind {
	case ovFiles:
		m.insertChoice(opt.value)
		m.closeOverlay()

	case ovCommands:
		// A skill is not a command: it is the opening of a message. The name
		// stays in the input so the task can be typed after it, and the worker
		// loads the instructions beside that message when it is sent.
		if name, ok := strings.CutPrefix(opt.value, skillPrefix); ok {
			m.input, m.cursor = []rune("/"+name+" "), 0
			m.cursor = len(m.input)
			m.closeOverlay()
			return
		}
		// The command is the whole message, so the line goes with the menu.
		m.input, m.cursor = m.input[:0], 0
		m.closeOverlay()
		m.runCommand(opt.value)

	case ovSessions:
		m.closeOverlay()
		m.loading = true
		m.command("session.load", map[string]any{"id": opt.value})

	case ovProviders:
		m.closeOverlay()
		m.command("provider.use", map[string]any{"name": opt.value})

	case ovModels:
		// The provider is unchanged; only the model moves. The worker rebuilds
		// the history for it, because a model swap can turn vision off and a
		// history full of screenshots would fail every request after it.
		m.closeOverlay()
		m.command("provider.set", map[string]any{"name": m.modelProvider, "model": opt.value})

	case ovVision:
		// An empty value is the auto row: the worker picks whatever fits.
		m.closeOverlay()
		m.command("vision.use", map[string]any{"name": opt.value})

	case ovEffort:
		m.closeOverlay()
		m.command("session.effort", map[string]any{"effort": opt.value})
	}
}
