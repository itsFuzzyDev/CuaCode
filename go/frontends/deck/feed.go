package main

// The feed is a list of blocks, not a list of lines. A block owns its own
// shape — prose wraps under a hanging indent, a batch of tool calls draws a
// box, thinking collapses to one line — and caches the rows it renders to
// until something invalidates them. Lines only ever come from blocks.
//
// One alignment rule holds the design together: every block's text starts in
// the same column, and only the two marker cells to its left change. The
// model's prose is the baseline and carries no marker at all, so everything
// that is *not* the answer — what you asked, what it thought, what it did to
// the machine — is what catches the eye.

import (
	"encoding/json"
	"fmt"
	"strings"
	"time"

	"cuacode/core/session"
)

type blockKind int

const (
	kHint blockKind = iota
	kUser
	kThink
	kProse
	kCalls
	kNotice
	kResumed
	kUltra
	kContext
	kUsage
)

type actState int

const (
	actPending actState = iota
	actOK
	actFail
)

// act is one tool call and, once it comes back, its result. res is the short
// text for the result column; note is the failure detail, which is too long to
// live in a column and gets its own rows underneath.
//
// arg is what the row shows; args is what was actually sent, kept verbatim for
// the inspector. index is where the worker filed the result, and what the
// inspector asks for it by — -1 until the call settles, because until then
// there is no record to ask about.
type act struct {
	name    string
	arg     string
	args    string // the call's arguments as JSON, as they crossed the wire
	index   int    // tool-record index, -1 while the call is still in flight
	res     string
	note    string
	state   actState
	settled time.Time // when the result landed, for the brief flash
}

type block struct {
	kind blockKind
	text string
	tone string // notice accent

	// What a user message had attached to it, by name. Names, because a
	// terminal cannot draw the picture and the filename is the whole of what
	// it can say — and because that is all a reopened conversation sends back.
	files []string

	acts       []act
	start, end time.Time
	open       bool // still collecting results — the header clock ticks

	// The two readouts that draw a page of numbers rather than text: what is in
	// the window now, and what every conversation has cost. Held as the worker
	// reported them, so a resize redraws the same reading rather than a stale
	// rendering of it.
	ctx   *ctxReport
	usage *usageReport

	// What this block cost and how fast it arrived. Only ever set on a thinking
	// block: the price of a thought is the one thing on screen that the text
	// itself cannot show, and it is also the part of a long wait people most
	// want an account of. Both move live while the round streams.
	tokens int
	tps    float64
	tokEst bool // ...and whether the count was estimated rather than billed

	rows  []string // cached rendering
	rowsW int      // width it was rendered at
	rowsF uint8    // toggle flags it was rendered at

	// Cached layout of text. Re-wrapping a whole streamed message on every
	// token that extends it is quadratic in the length of the message, which
	// is exactly the shape of lag that gets worse the more the model says. The
	// layout is therefore kept, keyed by the length it was built from, and
	// refreshed at reflowEvery rather than on every chunk.
	spans   [][]span
	lines   []string
	layoutW int
	layoutN int
	reflow  time.Time
}

func (b *block) touch() { b.rows = nil }

// stale reports whether the cached layout has to be rebuilt: never keep a
// layout at the wrong width, and while text is still arriving, rebuild at the
// reflow rate rather than on every chunk.
func (b *block) stale(width int, live bool, now time.Time) bool {
	switch {
	case b.spans == nil && b.lines == nil:
		return true
	case b.layoutW != width:
		return true
	case b.layoutN == len(b.text):
		return false
	case !live:
		return true
	}
	return !now.Before(b.reflow)
}

func (b *block) layoutDone(width int, now time.Time) {
	b.layoutW, b.layoutN, b.reflow = width, len(b.text), now.Add(reflowEvery)
}

func (b *block) elapsed() time.Duration {
	if b.open {
		return time.Since(b.start)
	}
	return b.end.Sub(b.start)
}

// push appends a block to the feed.
func (m *model) push(b *block) *block {
	m.blocks = append(m.blocks, b)
	return b
}

// tail returns the last block if it is of the given kind and still the one
// being written to, so streamed chunks extend it instead of stacking up.
func (m *model) tail(kind blockKind) *block {
	if n := len(m.blocks); n > 0 && m.blocks[n-1].kind == kind {
		return m.blocks[n-1]
	}
	return nil
}

// stream appends a streamed text chunk to the trailing block of that kind.
func (m *model) stream(kind blockKind, chunk string) {
	m.boundary()
	b := m.tail(kind)
	if b == nil {
		b = m.push(&block{kind: kind})
	}
	b.text += chunk
	b.touch()
}

// boundary closes the open batch once the model starts talking again. The
// worker puts no batch marker on the wire, so the turn from tool results back
// to prose is what separates one batch of calls from the next.
func (m *model) boundary() {
	if m.sawOutput {
		m.closeCalls()
	}
}

// openCalls returns the batch in progress, starting one if there is none.
func (m *model) openCalls() *block {
	if m.calls == nil {
		m.calls = m.push(&block{kind: kCalls, start: time.Now(), open: true})
	}
	return m.calls
}

func (m *model) closeCalls() {
	if m.calls != nil {
		m.calls.open = false
		m.calls.end = time.Now()
		for i := range m.calls.acts {
			if m.calls.acts[i].state == actPending {
				m.calls.acts[i].state, m.calls.acts[i].res = actFail, "no result"
				m.callFail++
			}
		}
		m.calls.touch()
		m.calls = nil
	}
	m.sawOutput = false
}

func (m *model) notice(tone, text string) {
	m.push(&block{kind: kNotice, tone: tone, text: text})
}

// reset empties the feed. Used when the conversation on screen stops being the
// conversation the worker holds — a new session, a loaded one, or /clear. The
// masthead is put back rather than cleared: it is what the screen looks like
// with nothing in it, and the keys are worth having in front of you at exactly
// the moment the screen just emptied.
func (m *model) reset() {
	m.blocks = m.blocks[:0]
	m.calls, m.sawOutput, m.callCount, m.callFail = nil, false, 0, 0
	// The calls on screen belonged to the conversation that just went, and so
	// do the record indices they were read by: a load renumbers them from the
	// history it replays.
	m.insp = inspector{}
	// The conversation on screen is not the one those tokens were spent on.
	// The next round reports its own, and until it does the gauge says nothing
	// rather than the last conversation's number.
	m.status.ContextUsed, m.status.ContextLeft = 0, 0
	// Attached to a message in a conversation that is no longer on screen.
	// Carrying them into the next one would send a picture to a session that
	// was never shown it.
	m.attach = nil
	m.scroll = 0
	m.needsClear = true
	m.push(&block{kind: kHint})
}

// effortOf reads the level back off a status. The worker is the authority on
// what it ended up as — it validates the value and can reject it — so the menu
// never records the choice itself.
func effortOf(data json.RawMessage) string {
	var reply struct {
		Effort string `json:"effort"`
	}
	_ = json.Unmarshal(data, &reply)
	return reply.Effort
}

// sessionInfo pulls the id and size out of a session status, for the line that
// says which conversation you are now looking at.
func sessionInfo(data json.RawMessage) (id string, msgs int) {
	var reply struct {
		ID   string `json:"session_id"`
		Msgs int    `json:"msg_count"`
	}
	if json.Unmarshal(data, &reply) != nil || reply.ID == "" {
		return "", 0
	}
	return reply.ID, reply.Msgs
}

// fold applies one worker event to the feed. core/session has already updated
// the snapshot; this only decides what the user sees.
func (m *model) fold(ev session.Event) {
	if ev.ParseErr != nil {
		m.notice(cErr, "unreadable worker line: "+clip(sanitize(string(ev.Raw)), 200))
		return
	}

	p := ev.Parsed
	switch p.State {
	case "startup", "ready":
		m.notice(cGhost, "welcome!")

	case "user":
		// Only ever seen while a reopened conversation replays: a live message
		// is echoed into the feed when it is sent, not when it comes back.
		m.boundary()
		m.closeCalls()
		m.push(&block{kind: kUser, text: p.Token, files: p.Images})

	case "thinking":
		m.stream(kThink, p.Token)

	case "content":
		m.stream(kProse, p.Token)

	case "tool_calls":
		m.boundary()
		calls := parseCalls(p.Token)
		b := m.openCalls()
		b.acts = append(b.acts, calls...)
		b.touch()
		m.callCount += len(calls)

	case "tool_output":
		m.settle(p.Token, ev)
		m.sawOutput = true

	case "background":
		// The call did not finish, it moved. Worth its own line: the row for it
		// is about to settle with a job id where a result belongs, and that
		// reads as a strange answer with nothing to explain it.
		m.notice(cCall, "backgrounded · "+p.Token+" is still running")

	case "notice":
		// Runtime text put into the conversation — neither the user's nor the
		// model's. Only the first paragraph is shown: the rest is instruction
		// addressed to the model, and on screen it would read as the agent
		// talking to itself.
		head, _, _ := strings.Cut(p.Token, "\n\n")
		m.boundary()
		m.notice(cCall, sanitize(head))

	// Mid-run readings. Nothing goes in the feed for either — the status bar
	// already moved — but they carry what the round's thinking is costing, and
	// the thinking they are pricing is on screen above. "rate" is the live
	// estimate, "usage" the count the provider actually charged.
	case "rate", "usage":
		m.priceThinking(ev)

	case "done":
		m.priceThinking(ev)
		m.closeCalls()
		m.finish()

	case "cancelled":
		m.closeCalls()
		m.notice(cWarn, "cancelled")
		m.finish()

	case "error":
		m.closeCalls()
		msg := p.Token
		if msg == "" {
			msg = p.Error
		}
		m.notice(cErr, "error: "+clip(sanitize(msg), 400))
		m.finish()

	case "session":
		// A session change replaces the conversation, so the feed goes with
		// it: what is on screen belongs to the session that was open. For a
		// load, the replayed conversation lands right behind this.
		id, msgs := sessionInfo(ev.Parsed.Data)
		// Effort belongs to the conversation, so it arrives with it.
		m.effort = effortOf(ev.Parsed.Data)
		m.reset()

		if !m.loading {
			m.notice(cGhost, "new session")
			break
		}
		// Said loudly, and in the past tense: everything under this line
		// already happened, and none of it is the agent working now.
		m.loading = false
		text := "resumed session " + id
		if msgs > 0 {
			text += sep + plural(msgs, "message", "messages")
		}
		m.push(&block{kind: kResumed, text: text})

	case "session_title":
		// The worker named the conversation, a turn or two after it started.
		// Said once, quietly: it changes nothing on screen, and the only reason
		// to mention it is so the name in the session picker later is not a
		// surprise.
		var reply struct {
			Title string `json:"title"`
		}
		if json.Unmarshal(p.Data, &reply) == nil && reply.Title != "" {
			m.notice(cGhost, "named · "+sanitize(reply.Title))
		}

	case "provider":
		// The switch is confirmed here rather than where it was asked for: the
		// worker resolves the model, and the status bar should say what it
		// resolved to, not what was requested.
		var reply struct {
			Provider string `json:"provider"`
			Model    string `json:"model"`
		}
		if json.Unmarshal(p.Data, &reply) == nil && reply.Provider != "" {
			m.provider, m.modelID = reply.Provider, reply.Model
		}
		m.notice(cGhost, strings.TrimSpace("now on "+m.provider+" "+shortModel(m.modelID)))

	case "effort":
		m.effort = effortOf(ev.Parsed.Data)
		level := m.effort
		if level == "" {
			level = "default"
		}
		m.notice(cGhost, "thinking effort: "+level)

	// "model" carries the raw provider chunk for debugging, and the bare
	// acknowledgements carry nothing worth a row. Both stay out of the feed.
	case "", "model", "stopped", "cancel_ack", "deleted":
	}
}

// priceThinking puts a round's thinking cost on the thinking it paid for.
//
// It runs twice over: once per live rate while the thought is still being
// written, and again from the provider's own count when the round is billed. So
// the figure is there during the wait, which is when it is worth having, and
// correct afterwards. The walk stops at the last user message — a round reports
// its own thinking, and an earlier turn's must never be relabelled with this
// one's number.
func (m *model) priceThinking(ev session.Event) {
	n, rate := ev.Parsed.ThinkTokens, ev.Parsed.ThinkTPS
	if n <= 0 && rate <= 0 {
		return
	}
	for i := len(m.blocks) - 1; i >= 0; i-- {
		b := m.blocks[i]
		if b.kind == kUser {
			return
		}
		if b.kind == kThink {
			if n > 0 {
				b.tokens, b.tokEst = n, ev.Parsed.ThinkEst
			}
			if rate > 0 {
				b.tps = rate
			}
			b.touch()
			return
		}
	}
}

// settle attaches a tool_output to the pending call it answers. Results come
// back in call order, so the first pending act with a matching name is the
// right one; an unmatched result still gets a row rather than vanishing.
func (m *model) settle(name string, ev session.Event) {
	res, note, ok := resultText(name, ev.Parsed.Data)
	state := actOK
	if !ok {
		state = actFail
		m.callFail++
	}
	// Where the worker filed the result. Sent with the event rather than
	// counted here: a frontend keeping its own tally would drift the first time
	// a round was rewound, and it would drift silently.
	index := recordIndex(ev.Parsed.Data)

	b := m.openCalls()
	defer b.touch()

	for i := range b.acts {
		if b.acts[i].state == actPending && b.acts[i].name == name {
			b.acts[i].res, b.acts[i].note, b.acts[i].state = res, note, state
			b.acts[i].index = index
			b.acts[i].settled = time.Now()
			return
		}
	}
	b.acts = append(b.acts, act{name: name, res: res, note: note, state: state,
		index: index, settled: time.Now()})
	m.callCount++
}

// recordIndex reads the tool-record index off a tool_output payload. A worker
// that does not send one leaves the call uninspectable rather than pointing the
// inspector at somebody else's result.
func recordIndex(data json.RawMessage) int {
	var payload struct {
		Index *int `json:"index"`
	}
	if json.Unmarshal(data, &payload) != nil || payload.Index == nil {
		return -1
	}
	return *payload.Index
}

// finish stops the run clock. The spinner is driven by session state; this is
// only the elapsed readout in the status bar.
func (m *model) finish() {
	if m.running {
		m.lastRun = time.Since(m.runStart)
		m.running = false
	}
}

// ---------------------------------------------------------------------------
// rendering

// live reports whether a block animates, and so cannot be cached: the batch
// still taking results, or the block the model is writing into right now.
// Everything above it in the feed is finished and renders once.
func (m *model) live(b *block) bool {
	if b.open {
		return true
	}
	// The two that are lit rather than written: they animate wherever they sit
	// in the feed, not just while they are the last thing in it.
	if b.kind == kUltra || (b.kind == kHint && m.ultra) {
		return true
	}
	n := len(m.blocks)
	return n > 0 && b == m.blocks[n-1] && session.Busy(m.status.State)
}

// rowsFor returns a block's rows, rendering only when the cache is stale — so
// an animation frame costs one block, not the whole feed.
func (m *model) rowsFor(b *block, flags uint8) []string {
	live := m.live(b)
	if b.rows != nil && b.rowsW == m.width && b.rowsF == flags && !live {
		return b.rows
	}
	rows := m.renderBlock(b, flags, live)
	if live {
		b.rows = nil
	} else {
		b.rows, b.rowsW, b.rowsF = rows, m.width, flags
	}
	return rows
}

func (m *model) renderBlock(b *block, flags uint8, live bool) []string {
	switch b.kind {
	case kHint:
		return m.renderHint()
	case kUser:
		return m.renderUser(b)
	case kProse:
		return m.renderProse(b, live)
	case kThink:
		return m.renderThinking(b, flags&flagThinking != 0, live)
	case kCalls:
		return m.renderCalls(b, flags&flagCalls != 0, flags&flagArgs != 0)
	case kNotice:
		return m.marked(b.text, "● ", b.tone, b.tone)
	case kResumed:
		return m.renderResumed(b)
	case kUltra:
		return m.renderUltra(b)
	case kContext:
		return m.renderContext(b)
	case kUsage:
		return m.renderUsage(b)
	}
	return nil
}

// renderProse lays out the model's own words: the formatted baseline of the
// feed, with no marker of its own. While it is still being written the last
// line carries the comet, which is the only moving thing on a quiet screen.
//
// An animation frame only re-renders that last line — the layout above it is
// held, so the cost of a frame is a line, not a message.
func (m *model) renderProse(b *block, live bool) []string {
	width := m.bodyW() - 3
	if now := time.Now(); b.stale(width, live, now) {
		b.spans = formatProse(tidy(b.text), width)
		b.layoutDone(width, now)
	}
	if len(b.spans) == 0 {
		return []string{margin + "  "}
	}

	rows := make([]string, 0, len(b.spans))
	for i, line := range b.spans {
		if live && i == len(b.spans)-1 {
			line = cometSpans(line, m.anim())
		}
		rows = append(rows, margin+"  "+renderSpans(line, cInk))
	}
	return rows
}

// ultracodeWord is the whole interface to it: type it to turn it on, type it
// again to turn it off. It is never sent anywhere.
const ultracodeWord = "ultracode"

// toggleUltra flips the mode and says so, loudly on the way in and quietly on
// the way out.
func (m *model) toggleUltra() {
	m.ultra = !m.ultra
	if m.ultra {
		m.push(&block{kind: kUltra, text: "ultracode engaged"})
		return
	}
	m.notice(cGhost, "ultracode off")
}

// renderUltra draws the banner for the mode. The rule either side is painted
// from the spectrum and the whole thing animates, because a hidden switch that
// announces itself quietly is not worth hiding.
func (m *model) renderUltra(b *block) []string {
	width := m.bodyW()
	label := "  " + strings.ToUpper(b.text) + "  "

	fill := width - vw(label)
	if fill < 2 {
		return []string{margin + gradient(trunc(label, width), ultraRamp, m.anim())}
	}
	left := fill / 2
	line := strings.Repeat("━", left) + label + strings.Repeat("━", fill-left)
	return []string{margin + gradient(line, ultraRamp, m.anim())}
}

// renderResumed draws the line that separates a conversation you are reading
// back from one you are having. It is a rule rather than a notice because
// everything below it is old, and that is a fact about the whole feed rather
// than about one moment in it.
func (m *model) renderResumed(b *block) []string {
	width := m.bodyW()
	label := trunc(b.text, max(width-8, 8))

	fill := width - vw(label) - 4
	if fill < 2 {
		return []string{margin + paint(cCall, label)}
	}
	left := fill / 2
	return []string{
		margin + paint(cRule, strings.Repeat("─", left)+"  ") +
			paint(cCall, label) +
			paint(cRule, "  "+strings.Repeat("─", fill-left)),
	}
}

func (m *model) renderHint() []string {
	keys := []string{"ctrl+t thinking", "tab tool calls", "shift+tab arguments", "ctrl+o inspect call", "ctrl+v attach image", "esc stop", "ctrl+c quit"}
	// Shown only while it would do something. A key that is listed and ignored
	// teaches people the app is unreliable, which is worse than not knowing the
	// key exists.
	if m.status.State == session.Tools {
		keys = append(keys[:6:6], "ctrl+b background", "ctrl+c quit")
	}

	name := paint(cMuted, "cuacode") + paint(cFaint, " · ") + paint(cGhost, "deck")
	if m.ultra {
		name = gradient("cuacode · deck", ultraRamp, m.anim())
	}
	return []string{
		margin + "  " + name,
		margin + "  " + paint(cGhost, strings.Join(keys, sep)),
	}
}

// names is the attachment list as the feed and the input row show it.
func names(atts []attachment) []string {
	if len(atts) == 0 {
		return nil
	}
	out := make([]string, 0, len(atts))
	for _, a := range atts {
		out = append(out, a.Name)
	}
	return out
}

// renderUser draws what the human said, and under it what they attached. The
// filenames get their own row rather than being folded into the sentence:
// they are not something the user typed, and a message that was nothing but a
// picture would otherwise have no rows at all.
func (m *model) renderUser(b *block) []string {
	rows := m.marked(b.text, "▌ ", cUser, cInk+bold)
	if len(b.files) == 0 {
		return rows
	}
	if b.text == "" {
		rows = nil // nothing was said; the pictures are the message
	}
	for _, name := range b.files {
		rows = append(rows, margin+paint(cUser, "▌ ")+paint(cMuted, "▣ "+name))
	}
	return rows
}

// marked wraps text under a two-cell marker that repeats down the left edge, so
// a wrapped paragraph still reads as one utterance.
func (m *model) marked(text, mark, markColor, textColor string) []string {
	width := m.bodyW() - vw(mark)
	body := squeeze(wrapPlain(tidy(text), width))
	if len(body) == 0 {
		body = []string{""}
	}

	rows := make([]string, 0, len(body))
	for _, line := range body {
		rows = append(rows, margin+paint(markColor, mark)+paint(textColor, line))
	}
	return rows
}

func (m *model) renderThinking(b *block, expanded, live bool) []string {
	const mark, label = "⋮ ", "thinking  "
	if expanded {
		return m.marked(b.text, mark, cFaint, cThink)
	}

	width := m.bodyW() - vw(mark)
	if now := time.Now(); b.stale(width, live, now) {
		b.lines = squeeze(wrapPlain(tidy(b.text), width))
		b.layoutDone(width, now)
	}
	body := b.lines
	if len(body) == 0 {
		return nil
	}

	// What it cost and what expanding would reveal. The cost goes first because
	// it is the one of the two that says whether the wait was worth anything;
	// the tilde marks a provider that bills thinking without itemising it, so
	// the figure is this end's estimate rather than what was charged.
	tag := ""
	if n := len(body) - 1; n > 0 {
		tag = plural(n, "more line", "more lines")
	}
	if b.tokens > 0 || b.tps > 0 {
		var cost []string
		if b.tokens > 0 {
			n := fmtTokens(b.tokens) + " tokens"
			if b.tokEst {
				n = "~" + n // this provider bills thinking without itemising it
			}
			cost = append(cost, n)
		}
		if b.tps > 0 {
			cost = append(cost, fmt.Sprintf("%.0f tok/s", b.tps))
		}
		if tag != "" {
			cost = append(cost, tag)
		}
		tag = strings.Join(cost, sep)
	}

	head := trunc(body[0], max(width-vw(label)-vw(tag)-2, 8))
	shown := paint(cGhost, head)
	if live {
		// Thought is the one thing with nothing to show for itself yet, so it
		// gets the shimmer while it lasts.
		shown = renderSpans(shimmerSpans([]span{{text: head}}, m.anim()), cGhost)
	}

	row := margin + paint(cFaint, mark) + paint(cThink, label) + shown
	if gap := 2 + m.bodyW() - vw(row) - vw(tag); tag != "" && gap > 1 {
		row += strings.Repeat(" ", gap) + paint(cGhost, tag)
	}
	return []string{row}
}

// renderCalls draws one batch of tool calls: what the agent did to the machine,
// in the order it did it, with what came back.
//
// No frame around it. The header carries the marker, the calls hang under it in
// the same text column as everything else, and the result column is what the
// eye actually follows down the right-hand side.
func (m *model) renderCalls(b *block, collapsed, fullArgs bool) []string {
	label := plural(len(b.acts), "tool call", "tool calls")
	meta := fmtDur(b.elapsed())

	tone := cFaint
	switch {
	case b.open:
		tone = pulse(cCall, cCallLit, m.anim(), 1100*time.Millisecond)
	case anyFailed(b.acts):
		tone = cErr
	}

	head := margin + paint(cRule, "▸ ") + paint(tone, label)
	if collapsed {
		names := make([]string, 0, len(b.acts))
		for _, a := range b.acts {
			names = append(names, a.name)
		}
		head += paint(cGhost, sep+meta)
		if list := trunc(strings.Join(names, ", "), max(2+m.bodyW()-vw(head)-vw(sep), 0)); list != "" {
			head += paint(cFaint, sep) + paint(cGhost, list)
		}
		return []string{head}
	}

	// The duration sits at the far right of the measure, over the result
	// column, so the batch reads as a table with a caption.
	if gap := 2 + m.bodyW() - vw(head) - vw(meta); gap > 1 {
		head += strings.Repeat(" ", gap) + paint(cGhost, meta)
	}

	width := m.bodyW() - 2
	if width < 26 {
		return m.narrowCalls(b, head)
	}

	rows := make([]string, 0, len(b.acts)+1)
	rows = append(rows, head)
	for _, a := range b.acts {
		rows = append(rows, margin+"  "+m.actRow(a, width, fullArgs))
		if fullArgs {
			rows = append(rows, m.argRows(a)...)
		}
		for _, line := range m.noteRows(a, width) {
			rows = append(rows, margin+"  "+line)
		}
	}
	return rows
}

// argRows spells a call's arguments out under its row, in the key column the
// permission prompt and the inspector both use. The row's own argument cell is
// a shape — enough to tell two calls apart at a glance — and this is the same
// call with nothing left out, for when the shape is not what you needed.
//
// They come from the tool_calls payload the feed already holds, so they are
// there for the call running right now as well as for every one behind it.
func (m *model) argRows(a act) []string {
	width := m.bodyW() - 2
	args := decodeArgs(json.RawMessage(a.args))
	if len(args) == 0 {
		if strings.TrimSpace(a.args) == "" {
			return nil
		}
		// Arguments that would not decode are still what was sent.
		return blockRows(plain(a.args), width, 1)
	}
	return valueRows(args, width, 0, langOf(text(args, "path")))
}

// nameCol is the width of the tool-name column. Every call in the toolbox fits
// it, so the arguments start in the same place on every row.
const nameCol = 12

// actRow lays out one call to exactly inner cells: name, argument, then the
// result flushed right.
//
// With the arguments spelled out underneath, the summary cell goes: the two say
// the same thing, and the shorter one saying it first only invites you to read
// the wrong one.
func (m *model) actRow(a act, inner int, fullArgs bool) string {
	res := m.resText(a)
	resW := min(max(vw(res), 2), 10)
	argW := inner - nameCol - resW - 2
	if fullArgs {
		argW = 0
	}

	body := paint(toolColor(a.name), padTo(trunc(a.name, nameCol-1), nameCol))
	if argW >= 6 {
		body += paint(cMuted, padTo(trunc(a.arg, argW), argW)) + "  "
	} else {
		resW = inner - nameCol
	}
	return body + paint(resColor(a), padLeft(trunc(res, resW), resW))
}

// resText is the result column. A call still in flight spins there, which is
// the only place the wait is visible per call rather than per run.
func (m *model) resText(a act) string {
	switch a.state {
	case actOK:
		return a.res
	case actFail:
		return "✗ " + a.res
	}
	return m.spinGlyph()
}

// noteRows are the continuation rows under a failed call, indented to the
// argument column. The message is what makes a failure actionable, and it
// never fits in the result column.
func (m *model) noteRows(a act, inner int) []string {
	if a.note == "" {
		return nil
	}

	indent := nameCol
	if inner-indent < 24 {
		indent = 0
	}
	width := inner - indent
	if width < 12 {
		return nil
	}

	body := wrapPlain(a.note, width)
	if len(body) > 3 {
		body = body[:3]
		body[2] = trunc(body[2], width-4) + " ..."
	}

	rows := make([]string, 0, len(body))
	for _, line := range body {
		if line == "" {
			continue
		}
		// No pad out to the width: nothing sits to the right of a note, and
		// padding it only puts spaces in the clipboard.
		rows = append(rows, strings.Repeat(" ", indent)+paint(cErr, line))
	}
	return rows
}

// narrowCalls is the fallback for a terminal with no room for columns: name
// and result only, one call per row.
func (m *model) narrowCalls(b *block, head string) []string {
	rows := []string{head}
	for _, a := range b.acts {
		row := margin + "  " + paint(toolColor(a.name), a.name) + "  " + paint(resColor(a), m.resText(a))
		rows = append(rows, trunc(row, m.width))
	}
	return rows
}

func anyFailed(acts []act) bool {
	for _, a := range acts {
		if a.state == actFail {
			return true
		}
	}
	return false
}

// toolColor groups the toolbox by what a call actually does: driving the
// machine, looking at it, or working off to the side.
func toolColor(name string) string {
	switch name {
	case "click", "type_text", "key", "scroll", "mouse_move":
		return cDrive
	case "screenshot", "photos", "app_list":
		return cLook
	}
	return cSide
}

// resColor tones the result column, lighting a result up for a moment as it
// lands so a batch of calls reads as a sequence of events rather than a table
// that appears all at once.
func resColor(a act) string {
	fresh := !a.settled.IsZero() && time.Since(a.settled) < flashSpan
	switch a.state {
	case actOK:
		if fresh {
			return cOKLit
		}
		return cOK
	case actFail:
		if fresh {
			return cErrLit
		}
		return cErr
	}
	return cCall
}

// tidy trims the blank edges a streamed block collects.
func tidy(s string) string { return strings.Trim(s, "\n ") }

// squeeze collapses runs of blank lines to one.
func squeeze(lines []string) []string {
	out := lines[:0:0]
	blank := false
	for _, l := range lines {
		empty := strings.TrimSpace(l) == ""
		if empty && blank {
			continue
		}
		blank = empty
		out = append(out, l)
	}
	return out
}
