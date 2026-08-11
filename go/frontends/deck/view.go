package main

// The frame: palette, measurement helpers, the status bar, the input row, and
// the composition of all three with the feed.

import (
	"fmt"
	"math"
	"strconv"
	"strings"
	"time"

	"cuacode/core/session"

	"github.com/charmbracelet/x/ansi"
	"github.com/charmbracelet/x/cellbuf"
)

// Palette. 256-colour so the greys have somewhere to live: the design leans on
// five levels of quiet (ink, muted, think, faint, ghost) and spends saturation
// only on the three things worth looking at — who is speaking, what a tool is,
// and whether it worked.
var (
	cInk   = fg(252)
	cMuted = fg(245)
	cThink = fg(243)
	cFaint = fg(240)
	cRule  = fg(238) // box borders: present, never competing with the content
	cGhost = fg(237)

	cUser    = fg(176) // the human's marker
	cCall    = fg(179) // a batch of tool calls, while it is still running
	cCallLit = fg(222) // the bright half of its pulse

	cDrive = fg(80)  // tools that move the machine
	cLook  = fg(111) // tools that observe it
	cSide  = fg(139) // everything else

	cOK   = fg(114)
	cWarn = fg(179)
	cErr  = fg(203)

	// The lit variants a result wears for the moment after it arrives.
	cOKLit  = fg(120)
	cErrLit = fg(210)

	// Inline prose styles.
	sBold   = fg(255) + bold
	sItalic = fg(252) + italic
	sCode   = fg(216)
	sHead   = fg(255) + bold

	// Source, for the one place the screen shows code rather than talks about
	// it: the patch on a write or edit prompt. Four colours and a quiet one for
	// punctuation — enough to give a hunk its shape, few enough that the diff's
	// own colours still read as the loudest thing on it.
	sKeyword = fg(140)
	sString  = fg(107)
	sNumber  = fg(173)
	sComment = fg(243)
	sPunct   = fg(245)

	// The shimmer band. Soft at both edges: a hard-edged block marching two
	// cells at a time reads as stutter, the same block with a gradient in
	// front of it and behind it reads as a glide.
	shimmerRamp = []string{
		fg(240), fg(243), fg(246), fg(249), fg(252), fg(255),
		fg(252), fg(249), fg(246), fg(243), fg(240),
	}

	// The comet: the newest characters of a line still being written, brightest
	// at the end. It does not animate — the text arriving underneath it is the
	// movement, and nothing looks laggier than an effect racing the content.
	cometRamp = []string{fg(246), fg(248), fg(250), fg(252), fg(254), fg(255)}

	// Ash into ember and back. Worn by the top of the effort ladder, which is
	// the setting that costs you the most time.
	emberRamp = []string{
		fg(240), fg(244), fg(248), fg(252), fg(224), fg(217),
		fg(210), fg(203), fg(196), fg(160), fg(124), fg(88),
		fg(124), fg(160), fg(203), fg(210), fg(217), fg(248),
	}

	// The full spectrum, launched from purple and coming back to it. Only ever
	// seen when ultracode is on, which is the entire point of it.
	ultraRamp = []string{
		fg(129), fg(135), fg(141), fg(177), fg(213), fg(201),
		fg(198), fg(197), fg(203), fg(209), fg(214), fg(220),
		fg(190), fg(118), fg(84), fg(49), fg(45), fg(39),
		fg(63), fg(99),
	}

	barBG = bg(236)
)

const (
	bold      = "\x1b[1m"
	italic    = "\x1b[3m"
	styleOff  = "\x1b[22;23;39m" // weight, slant and foreground — never the background
	bgOff     = "\x1b[49m"       // ...and the one escape that does close a background
	reverse   = "\x1b[7m"
	revOff    = "\x1b[27m"
	reset     = "\x1b[0m"
	flashSpan = 600 * time.Millisecond // how long a fresh result stays lit

	// How often a block still receiving text is re-wrapped. Tokens arrive far
	// faster than anyone reads, so laying the message out afresh for each one
	// buys nothing and costs more the longer the message gets.
	reflowEvery = 70 * time.Millisecond
)

// margin is the feed's left inset; every row starts with it.
const (
	margin  = "  "
	marginW = 4  // margin plus the same gap on the right
	measure = 82 // widest a block may be, however wide the terminal is
	sep     = "  ·  "
)

// bodyW is the shared measure every block renders to, so the feed has one right
// edge as well as one left one — prose stops where the boxes stop.
func (m *model) bodyW() int { return min(m.width-marginW, measure) }

// Cache keys for the two things a keypress can change about a rendering.
const (
	flagThinking uint8 = 1 << iota
	flagCalls
	flagUltra
	flagArgs
)

var spinFrames = []string{"⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"}

func fg(n int) string { return "\x1b[38;5;" + strconv.Itoa(n) + "m" }
func bg(n int) string { return "\x1b[48;5;" + strconv.Itoa(n) + "m" }

// paint styles a run of text and closes by resetting only weight and
// foreground, never the background, so it composes inside the status bar.
// style may be a concatenation, e.g. cInk+bold.
func paint(style, s string) string {
	if s == "" {
		return ""
	}
	return style + s + styleOff
}

// vw is the width of a string in terminal cells, ignoring escape sequences.
func vw(s string) int { return ansi.StringWidth(s) }

// shimmerCells is how fast the band travels, in cells per second. Taken from
// the clock rather than the frame counter, so the glide is the same speed
// whatever the repaint rate happens to be — a dropped frame costs a frame, not
// a hitch in the animation.
const shimmerCells = 24.0

// shimmerSpans lays the moving band over the spans of a line that carry no
// style of their own, so emphasis and code keep theirs. The band runs off the
// end and comes straight back, with no dead phase where nothing is lit.
func shimmerSpans(line []span, elapsed time.Duration) []span {
	width := spanWidth(line)
	if width == 0 {
		return line
	}

	// The band wraps around the line rather than running off the end and
	// starting over: as its tail leaves on the right its head is already
	// re-entering on the left, so there is no frame where nothing is lit.
	cycle := float64(width)
	head := math.Mod(elapsed.Seconds()*shimmerCells, cycle)

	out := make([]span, 0, width+len(line))
	col := 0
	for _, sp := range line {
		if sp.style != "" {
			out, col = append(out, sp), col+vw(sp.text)
			continue
		}
		for _, r := range sp.text {
			style := ""
			if d := int(math.Mod(float64(col)-head+cycle, cycle)); d < len(shimmerRamp) {
				style = shimmerRamp[d]
			}
			out = append(out, span{text: string(r), style: style})
			col++
		}
	}
	return out
}

// cometSpans brightens the tail of a line that is still being written and puts
// a soft block where the next character will land.
func cometSpans(line []span, elapsed time.Duration) []span {
	out := make([]span, 0, len(line)+len(cometRamp)+1)
	tail := max(spanWidth(line)-len(cometRamp), 0)

	col := 0
	for _, sp := range line {
		if sp.style != "" || col+vw(sp.text) <= tail {
			out, col = append(out, sp), col+vw(sp.text)
			continue
		}
		for _, r := range sp.text {
			style := ""
			if d := col - tail; d >= 0 && d < len(cometRamp) {
				style = cometRamp[d]
			}
			out = append(out, span{text: string(r), style: style})
			col++
		}
	}
	return append(out, span{text: "▌", style: pulse(cFaint, cInk, elapsed, 700*time.Millisecond)})
}

// gradientCells is how fast a gradient travels along the text it colours.
const gradientCells = 9.0

// gradient lays a moving colour ramp along a string, one colour per cell. It
// writes an escape per character, so it is for labels and bars — a few dozen
// cells — and never for prose.
func gradient(s string, ramp []string, elapsed time.Duration) string {
	if len(ramp) == 0 || s == "" {
		return s
	}
	head := elapsed.Seconds() * gradientCells
	n := float64(len(ramp))

	var b strings.Builder
	b.Grow(len(s) * 12)
	for i, r := range []rune(s) {
		idx := int(math.Mod(math.Mod(float64(i)-head, n)+n, n))
		b.WriteString(ramp[idx])
		b.WriteRune(r)
		_ = i
	}
	b.WriteString(styleOff)
	return b.String()
}

// pulse alternates between two tones on a fixed period — used where a spinner
// would be too loud but something still has to say "running".
func pulse(dim, bright string, elapsed, period time.Duration) string {
	if period <= 0 || (elapsed/period)%2 == 0 {
		return bright
	}
	return dim
}

func padTo(s string, w int) string {
	if pad := w - vw(s); pad > 0 {
		return s + strings.Repeat(" ", pad)
	}
	return s
}

func padLeft(s string, w int) string {
	if pad := w - vw(s); pad > 0 {
		return strings.Repeat(" ", pad) + s
	}
	return s
}

func trunc(s string, w int) string {
	if w < 1 {
		return ""
	}
	if vw(s) <= w {
		return s
	}
	return ansi.Truncate(s, w, "...")
}

func wrapPlain(s string, w int) []string {
	if w < 4 {
		w = 4
	}
	var out []string
	for _, para := range strings.Split(s, "\n") {
		if para == "" {
			out = append(out, "")
			continue
		}
		out = append(out, strings.Split(cellbuf.Wrap(para, w, ""), "\n")...)
	}
	return out
}

func itoa(n int) string { return strconv.Itoa(n) }

func plural(n int, one, many string) string {
	if n == 1 {
		return "1 " + one
	}
	return itoa(n) + " " + many
}

// fmtDur keeps three significant characters so the column never jitters.
func fmtDur(d time.Duration) string {
	switch s := d.Seconds(); {
	case d < 0:
		return "0.0s"
	case s < 10:
		return fmt.Sprintf("%.1fs", s)
	case s < 60:
		return fmt.Sprintf("%.0fs", s)
	default:
		return fmt.Sprintf("%dm%02ds", int(s)/60, int(s)%60)
	}
}

func stateColor(st session.State) string {
	switch st {
	case session.Running:
		return cLook
	case session.Tools:
		return cCall
	case session.Done:
		return cOK
	case session.Error:
		return cErr
	case session.Cancelled:
		return cWarn
	}
	return cMuted
}

// eighths are the partial cells the gauge ends on, so a meter ten cells wide
// still moves when a percent goes by rather than sitting still for ten of them.
var eighths = []string{"", "▏", "▎", "▍", "▌", "▋", "▊", "▉"}

// fmtTokens keeps a token count to three characters or so, so the readout next
// to the gauge never changes width while a run is spending it.
func fmtTokens(n int) string {
	switch {
	case n >= 1_000_000:
		return fmt.Sprintf("%.1fM", float64(n)/1e6)
	case n >= 10_000:
		return itoa(n/1000) + "k"
	case n >= 1_000:
		return fmt.Sprintf("%.1fk", float64(n)/1000)
	}
	return itoa(n)
}

// gauge draws the context meter, as spent out of the whole.
//
// The worker reports what a round actually cost and, when it knows the model's
// window, the size of it. When it does not — an unlisted model, a local one
// nobody has told it about — there is no denominator to draw against, and the
// gauge says how much has been spent rather than inventing a total to divide by.
// A meter reading against a made-up number is worse than no meter.
func (m *model) gauge() string {
	used, total := m.status.ContextUsed, m.ctxMax
	if used <= 0 && total > 0 && m.status.ContextLeft > 0 {
		// A worker that only reports what is left still gets a meter: the two
		// readings are the same fact from opposite ends.
		used = total - m.status.ContextLeft
	}
	if used <= 0 {
		return ""
	}
	if total <= 0 {
		return paint(cMuted, "ctx ") + paint(cMuted, fmtTokens(used))
	}

	const cells = 10
	frac := min(max(float64(used)/float64(total), 0), 1)

	// The bar fills as the window is spent, so a full bar is the thing to worry
	// about — and the colour arrives before it does.
	tone, num := cOK, cMuted
	switch {
	case frac > 0.85:
		tone, num = cErr, cErr
	case frac > 0.65:
		tone, num = cWarn, cWarn
	}

	// It fills in whole cells and finishes on a fraction of one, so a percent
	// moves it rather than ten of them going by with nothing happening.
	filled := frac * cells
	full := int(filled)
	part := eighths[int((filled-float64(full))*8)]
	empty := cells - full
	if part != "" {
		empty--
	}

	bar := paint(tone, strings.Repeat("█", full)+part) + paint(cGhost, strings.Repeat("░", max(empty, 0)))
	return paint(cMuted, "ctx ") + bar +
		paint(num, " "+fmtTokens(used)) + paint(cFaint, "/"+fmtTokens(total)) +
		paint(cFaint, fmt.Sprintf(" %d%%", int(frac*100)))
}

// shortModel is the model id with the parts nobody reads taken off: the vendor
// namespace in front of it, and the release date providers stamp on the end.
// What is left is the thing you actually chose between in the picker.
func shortModel(id string) string {
	if i := strings.LastIndexByte(id, '/'); i >= 0 {
		id = id[i+1:]
	}
	if i := strings.LastIndexByte(id, '-'); i > 0 && len(id)-i == 9 {
		if _, err := strconv.Atoi(id[i+1:]); err == nil {
			id = id[:i]
		}
	}
	return id
}

// modelChip says who is answering. It is the one thing the bar carries that is
// not about this run: everything else changes minute to minute, and this only
// changes when you change it — which is exactly why it is worth having in front
// of you when you read the rest.
func (m *model) modelChip() string {
	name := shortModel(m.modelID)
	switch {
	case name == "" && m.provider == "":
		return ""
	case name == "":
		return paint(cGhost, m.provider)
	case m.provider == "":
		return paint(cMuted, name)
	}
	return paint(cGhost, m.provider+" ") + paint(cMuted, name)
}

// rateSeg is how fast the round is generating, beside the elapsed clock: the
// two together are the difference between "this model is slow" and "this turn
// was long", which the clock on its own cannot tell you.
//
// It is live. While the round streams, the worker estimates the rate from the
// characters it has sent, and a tilde says so; when the provider bills the round
// the measured figure replaces it and the tilde goes. The distinction is worth
// the one cell it costs — an estimate that presented itself as a measurement
// would be believed.
func (m *model) rateSeg() string {
	st := m.status
	if st.TPS <= 0 {
		return ""
	}
	rate := fmt.Sprintf("%.0f tok/s", st.TPS)
	if st.TPSEst {
		rate = "~" + rate
	}
	out := paint(cMuted, rate)
	// Thinking is the half of a reply nobody reads and everybody waits for, so
	// what it cost is worth carrying next to the rate rather than buried in
	// /context. Its own rate stays on the thinking row, where the text is.
	if n := st.ThinkTokens; n > 0 {
		tag := fmtTokens(n) + " think"
		if st.ThinkEst {
			tag = "~" + tag
		}
		out += paint(cGhost, barSep) + paint(cThink, tag)
	}
	return out
}

// statePill is the one loud thing on the bar. Reversed rather than coloured:
// the state is the answer to "can I type yet", and it should be findable
// without reading anything.
func (m *model) statePill(st session.State) string {
	label := " " + string(st) + " "
	if m.ultra {
		return reverse + gradient(label, ultraRamp, m.anim()) + revOff
	}
	return stateColor(st) + reverse + label + revOff + styleOff
}

// seg is one reading on the bar, with how readily it gives up its cells. A
// narrow terminal drops the highest number first and keeps going until the two
// halves fit; drop 0 never leaves, so the state survives any width.
type seg struct {
	text string
	drop int
	gone bool // trimmed away at this width
}

// barSep is tighter than the feed's separator. The bar carries a dozen readings
// where a line of prose carries two, and at five cells a piece the dividers cost
// more room than several of the things they divide.
const barSep = " · "

// barGap is the least space the two halves keep between them before the bar is
// allowed to take anything else back in.
const barGap = 4

func joinSegs(segs []seg) string {
	var b strings.Builder
	for _, s := range segs {
		if s.text == "" || s.gone {
			continue
		}
		if b.Len() > 0 {
			b.WriteString(paint(cGhost, barSep))
		}
		b.WriteString(s.text)
	}
	return b.String()
}

// pick walks both halves and returns the reading that best answers want: the
// most expendable one still showing, or the least expendable one already gone.
// Nothing with drop 0 is ever a candidate.
func pick(left, right []seg, gone bool) *seg {
	var found *seg
	for _, half := range [][]seg{left, right} {
		for i := range half {
			s := &half[i]
			switch {
			case s.drop == 0 || s.gone != gone:
			case found == nil:
				found = s
			case gone && s.drop < found.drop:
				found = s
			case !gone && s.drop > found.drop:
				found = s
			}
		}
	}
	return found
}

// fitBar trims the bar to the terminal and then puts back what the trimming
// overshot. Trimming is coarse — one reading can free far more room than was
// missing — so a second pass offers everything that was dropped back in order
// of how much it was worth keeping, and takes whatever still fits. Without it a
// wide bar can lose a long reading to a shortfall of three cells.
func (m *model) fitBar(left, right []seg) (row, rhs string, gap int) {
	measure := func() (string, string, int) {
		l, r := margin+joinSegs(left), joinSegs(right)
		return l, r, m.width - 2 - vw(l) - vw(r)
	}

	row, rhs, gap = measure()
	for gap < 1 {
		s := pick(left, right, false)
		if s == nil {
			return row, rhs, gap
		}
		s.gone = true
		row, rhs, gap = measure()
	}

	// Each reading is offered once, in order of what it was worth, and a short
	// one can still get in after a longer one was turned away. drop 0 marks it
	// answered either way, so pick moves on.
	for s := pick(left, right, true); s != nil; s = pick(left, right, true) {
		s.gone = false
		// Restoring asks for more than the one cell that makes a bar legal: a
		// reading put back so tightly against the other half that they read as
		// one run of text is worse than the gap it filled.
		if l, r, g := measure(); g >= barGap {
			row, rhs, gap = l, r, g
		} else {
			s.gone = true
		}
		s.drop = 0
	}
	return row, rhs, gap
}

// renderStatus draws the bar between feed and input. The left half is this run
// — what it is doing, for how long, and how much of it went wrong — and the
// right half is the conversation it belongs to.
func (m *model) renderStatus() string {
	st := m.status.State
	if st == "" {
		st = session.Idle
	}

	// One cell, whether or not it is spinning: the pill must not move when a run
	// starts, or the eye follows the jump instead of the state.
	spin := " "
	if session.Busy(st) {
		spin = m.spinGlyph()
	}

	left := []seg{{text: spin + m.statePill(st)}}

	if m.callCount > 0 {
		calls := paint(cMuted, plural(m.callCount, "call", "calls"))
		// Failures are counted rather than listed: the feed has the detail, and
		// the bar only has to make you go and look.
		if m.callFail > 0 {
			calls += paint(cGhost, barSep) + paint(cErr, itoa(m.callFail)+" failed")
		}
		left = append(left, seg{text: calls, drop: 2})
	}
	if d := m.runtime(); d > 0 {
		left = append(left, seg{text: paint(cMuted, fmtDur(d)), drop: 3})
	}
	if rate := m.rateSeg(); rate != "" {
		left = append(left, seg{text: rate, drop: 4})
	}
	// How far back you are looking, so a feed that is not moving while tokens
	// arrive reads as scrolled rather than as stalled.
	if m.scroll > 0 {
		left = append(left, seg{text: paint(cWarn, "scrolled "+itoa(m.scroll)), drop: 3})
	}
	if n := len(m.permQueue); n > 0 {
		left = append(left, seg{text: paint(cCall, plural(n, "question", "questions")+" waiting"), drop: 1})
	}
	// Only worth a word when it is off: asking is the default, and running
	// file and shell calls unattended is the state worth noticing.
	if !m.askMode {
		left = append(left, seg{text: paint(cWarn, "no prompts"), drop: 1})
	}
	// Likewise the effort: shown only once it has been moved off whatever the
	// provider does by default, because that is when it explains the wait.
	if m.effort != "" {
		left = append(left, seg{text: paint(cMuted, "effort ") + paint(cInk, m.effort), drop: 4})
	}
	// The two toggles, shown only in the state you have to have pressed a key to
	// get to — so a feed that looks wrong has its reason on the bar.
	if m.showThinking {
		left = append(left, seg{text: paint(cGhost, "thinking"), drop: 6})
	}
	if m.foldCalls {
		left = append(left, seg{text: paint(cGhost, "folded"), drop: 6})
	}

	var right []seg
	if chip := m.modelChip(); chip != "" {
		right = append(right, seg{text: chip, drop: 5})
	}
	if g := m.gauge(); g != "" {
		right = append(right, seg{text: g, drop: 3})
	}
	right = append(right, seg{text: paint(cMuted, plural(m.status.Msgs, "msg", "msgs")), drop: 6})
	if m.status.Turns > 0 {
		right = append(right, seg{text: paint(cMuted, plural(m.status.Turns, "turn", "turns")), drop: 7})
	}
	// The clock: a long run is easier to place against the time of day than
	// against its own elapsed counter. First to go, and no loss when it does.
	right = append(right, seg{text: paint(cFaint, time.Now().Format("15:04")), drop: 8})

	row, rhs, gap := m.fitBar(left, right)
	if gap < 1 {
		// Too narrow even for the state on its own: what is left of it, padded
		// out, because the bar's blanks are the bar.
		return barBG + padTo(trunc(row, m.width), m.width) + reset
	}
	return barBG + row + strings.Repeat(" ", gap) + rhs + "  " + reset
}

// inputMark is the prompt; its width is also the indent every continuation
// line of the input gets, so a wrapped message stays in one column.
const inputMark = "› "

// inputLines lays the buffer out the way it will be drawn: split on the
// newlines the user inserted, then wrapped to the width, with the cursor
// located among them. Returns the lines and the cursor's row and column.
func (m *model) inputLines() (lines [][]rune, row, col int) {
	width := max(m.width-marginW-vw(inputMark), 1)
	row, col = 0, 0

	var cur []rune
	place := func(i int) {
		if i == m.cursor {
			row, col = len(lines), len(cur)
		}
	}

	for i, r := range m.input {
		place(i)
		if r == '\n' {
			lines = append(lines, cur)
			cur = nil
			continue
		}
		cur = append(cur, r)
		if len(cur) == width {
			lines = append(lines, cur)
			cur = nil
		}
	}
	place(len(m.input))
	return append(lines, cur), row, col
}

// inputHeight is how many rows the input needs, capped so a long paste cannot
// push the feed off the screen.
func (m *model) inputHeight() int {
	lines, _, _ := m.inputLines()
	return min(max(len(lines), 1), maxInputRows)
}

const maxInputRows = 6

// renderInput draws the input, one row per line, with the cursor shown as a
// block. Only the last maxInputRows are drawn, so the cursor stays visible
// however much has been typed.
func (m *model) renderInput() []string {
	// While the inspector is up there is nothing to type into, so the row says
	// what the keys do instead of drawing a prompt that ignores them.
	if m.insp.on {
		return m.inspectKeys()
	}
	lines, curRow, curCol := m.inputLines()

	start := 0
	if len(lines) > maxInputRows {
		start = len(lines) - maxInputRows
		if curRow < start {
			start = curRow
		}
	}

	rows := make([]string, 0, maxInputRows)
	for i := start; i < len(lines) && i-start < maxInputRows; i++ {
		var b strings.Builder
		b.WriteString(margin)
		if i == start {
			if m.ultra {
				b.WriteString(gradient(inputMark, ultraRamp, m.anim()))
			} else {
				b.WriteString(paint(cDrive, inputMark))
			}
		} else {
			b.WriteString(strings.Repeat(" ", vw(inputMark)))
		}

		line := lines[i]
		if i == curRow {
			under, rest := " ", ""
			if curCol < len(line) {
				under, rest = string(line[curCol]), string(line[curCol+1:])
			}
			b.WriteString(paint(cInk, string(line[:curCol])))
			b.WriteString(reverse + under + revOff)
			b.WriteString(paint(cInk, rest))
		} else {
			b.WriteString(paint(cInk, string(line)))
		}

		if i == curRow && len(m.input) == 0 && session.Busy(m.status.State) {
			hint := " esc to stop"
			if m.status.State == session.Tools {
				hint += "  ·  ctrl+b to background"
			}
			b.WriteString(paint(cGhost, hint))
		}
		rows = append(rows, b.String())
	}
	return rows
}

// row writes one line, trimmed to the terminal width and no further.
//
// It deliberately stops at the end of the text rather than padding out to the
// right edge. Padding was there to keep a repaint from leaving stale cells
// behind, but the renderer already erases to the end of a line whose tail has
// gone blank — and a line padded with real spaces is a line whose spaces the
// terminal hands over when you select it, so every copied paragraph came out
// dragging a rectangle of whitespace behind it.
//
// Trailing spaces go with it, wherever in the styling they happen to sit —
// except the ones that are visible in their own right. The status bar pads
// itself and its blanks carry a background; the cursor is a space in reverse
// video. Both are content, not padding.
func (m *model) row(b *strings.Builder, s string) {
	b.WriteString(trimTrailing(trunc(s, m.width)))
	b.WriteString(reset)
}

// trimTrailing drops the spaces at the end of a row without disturbing the
// escapes among them: a column pad written inside a colour still closes that
// colour, so the run has to be kept even once its spaces are gone. Only spaces
// are dropped, and only the ones with nothing printable after them.
//
// A space under a background or under reverse video paints a cell of its own and
// stays: that is the status bar's fill, and the block the cursor is drawn as.
//
// It works by finding where the row's last visible cell ends and keeping every
// byte up to there, then the closing escapes that follow. Nothing is reordered.
// Emitting an escape ahead of a space still waiting to be written would move the
// space into the next run's styling, which for the cursor's reverse video means a
// second block of it for every space typed in front of it.
func trimTrailing(s string) string {
	cut, lit := 0, false
	for i := 0; i < len(s); {
		switch {
		case s[i] == 0x1b:
			j := i + 1
			for j < len(s) && s[j] != 'm' {
				j++
			}
			if j < len(s) {
				j++
			}
			if esc := s[i:j]; strings.Contains(esc, "48;") || esc == reverse {
				lit = true
			} else if esc == reset || esc == revOff || strings.Contains(esc, "49m") {
				lit = false
			}
			i = j

		case s[i] == ' ' && !lit:
			i++

		default:
			i++
			cut = i
		}
	}
	if cut == len(s) {
		return s
	}

	var b strings.Builder
	b.Grow(len(s))
	b.WriteString(s[:cut])

	// Past the cut only the escapes survive, so a style the row opened is still
	// closed by the row.
	for i := cut; i < len(s); {
		if s[i] != 0x1b {
			i++
			continue
		}
		j := i + 1
		for j < len(s) && s[j] != 'm' {
			j++
		}
		if j < len(s) {
			j++
		}
		b.WriteString(s[i:j])
		i = j
	}
	return b.String()
}

// layout divides the screen: the chrome takes what it needs, the feed gets what
// is left, and the three always sum to exactly the terminal height.
//
// Nothing may push the frame past that height. A frame one row too tall makes
// the terminal scroll, which moves every earlier row up instead of over-writing
// it — so the screen fills with the wreckage of previous frames and no amount
// of redrawing takes them away. The chrome is therefore trimmed until it fits,
// rather than the feed being clamped to a minimum and the total left to grow.
func (m *model) layout() (feedH int, overlay, input []string) {
	overlay, input = m.renderOverlay(), m.renderInput()

	// One blank row above the chrome, one status bar.
	const fixed = 2
	for len(overlay)+len(input)+fixed >= m.height {
		switch {
		case len(overlay) > 0:
			overlay = overlay[:len(overlay)-1]
		case len(input) > 1:
			input = input[:len(input)-1]
		default:
			return 1, nil, input[:1]
		}
	}
	return m.height - fixed - len(overlay) - len(input), overlay, input
}

// render composes the frame: feed, status bar, input row.
func (m *model) render() string {
	if m.width == 0 || m.height == 0 {
		return "loading..."
	}
	if m.height < 4 || m.width < 20 {
		return "terminal too small\n"
	}

	var b strings.Builder

	h, overlay, input := m.layout()

	// The feed fills from the top and grows down, the way a log does. Once it
	// outgrows the area the window follows the end of it instead — so its
	// scroll is measured back from the end.
	//
	// The inspector is the other kind of thing: a page, which starts at its
	// first line and is read downwards, so its scroll is measured from the top.
	rows := m.viewRows()
	start := max(len(rows)-h-m.scroll, 0)
	if m.insp.on {
		start = min(max(m.insp.scroll, 0), max(len(rows)-h, 0))
	}
	for i := range h {
		if idx := start + i; idx < len(rows) {
			m.row(&b, rows[idx])
		} else {
			m.row(&b, "")
		}
		b.WriteByte('\n')
	}

	// The feed never touches the chrome below it: one blank row always sits
	// between the last thing the agent said and the status bar, whether or not
	// a menu is in between.
	m.row(&b, "")
	b.WriteByte('\n')

	for _, row := range overlay {
		m.row(&b, row)
		b.WriteByte('\n')
	}

	m.row(&b, m.renderStatus())
	for _, row := range input {
		b.WriteByte('\n')
		m.row(&b, row)
	}

	return b.String()
}
