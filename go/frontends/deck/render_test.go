package main

import (
	"regexp"
	"strconv"
	"strings"
	"testing"
	"time"

	tea "charm.land/bubbletea/v2"

	"cuacode/core/protocol"
	"cuacode/core/session"
)

// A scripted run in the worker's own wire format: two batches of tool calls
// around streamed prose, one failing call, one oversized argument, and a line
// that isn't JSON at all.
var script = []string{
	`{"type":"status","id":"boot","data":{"state":"ready"}}`,
	`{"type":"token","id":"msg-1","data":{"state":"thinking","token":"safari is probably not focused yet, so open it first and take a look before clicking anything","status":"running"}}`,
	`{"type":"token","id":"msg-1","data":{"state":"content","token":"I'll open Safari ","status":"running"}}`,
	`{"type":"token","id":"msg-1","data":{"state":"content","token":"and run the search.","status":"running"}}`,
	`{"type":"token","id":"msg-1","data":{"state":"tool_calls","token":[{"function":{"name":"app_open","arguments":{"app":"Safari"}}},{"function":{"name":"screenshot","arguments":{}}},{"function":{"name":"click","arguments":{"x":612,"y":84,"clicks":2}}}],"status":"tooling"}}`,
	`{"type":"token","id":"msg-1","data":{"state":"tool_output","token":"app_open","result":{"result":{"ok":true,"app":"Safari"}},"status":"tooling"}}`,
	`{"type":"token","id":"msg-1","data":{"state":"tool_output","token":"screenshot","result":{"result":{"n":1}},"status":"tooling"}}`,
	`{"type":"token","id":"msg-1","data":{"state":"tool_output","token":"click","result":{"error":"screen coordinates out of range for display 0"},"status":"tooling"}}`,
	`{"type":"token","id":"msg-1","data":{"state":"content","token":"Address bar is focused. Typing the query now — this line is deliberately long so it has to wrap under its gutter at every width the test tries.","status":"running"}}`,
	`{"type":"token","id":"msg-1","data":{"state":"tool_calls","token":[{"id":"call_1","type":"function","function":{"name":"type_text","arguments":"{\"text\":\"otters, and a good deal more text than fits in a narrow column\"}"}},{"type":"tool_use","id":"call_2","name":"key","input":{"combo":"Return"}}],"status":"tooling"}}`,
	`{"type":"token","id":"msg-1","data":{"state":"tool_output","token":"type_text","result":{"result":{"typed":"otters"}},"status":"tooling"}}`,
	`{"type":"token","id":"msg-1","data":{"state":"tool_output","token":"key","result":{"result":{"pressed":"Return"}},"status":"tooling"}}`,
	`not json at all`,
	contentEvent(mdProse),
	`{"type":"token","id":"msg-1","data":{"state":"done","token":"done","status":"done","msg_count":12}}`,
}

// Markdown the model might plausibly send back. \x60 is a backtick, which a raw
// string literal cannot hold.
var mdProse = "Done — the results are on screen.\n\n" +
	"## What I did\n\n" +
	"- opened \x60Safari\x60 with **app_open**\n" +
	"- typed the query, then pressed \x60Return\x60\n\n" +
	"The *click* at (612, 84) failed, so nothing was clicked there:\n\n" +
	"\x60\x60\x60\nclick(x=612, y=84)  -> out of range\n\x60\x60\x60\n"

func contentEvent(text string) string {
	return `{"type":"token","id":"msg-1","data":{"state":"content","token":` +
		strconv.Quote(text) + `,"status":"running"}}`
}

func parse(t *testing.T, raw string) session.Event {
	t.Helper()
	p, err := protocol.ParseEvent([]byte(raw))
	return session.Event{Parsed: p, ParseErr: err, Raw: []byte(raw)}
}

// play runs the script through a model sized w x h and returns it.
func play(t *testing.T, w, h int) *model {
	t.Helper()
	m := initialModel()
	m.width, m.height = w, h
	m.status = session.Snapshot{State: session.Tools, Msgs: 12, Turns: 2, ContextLeft: 24000}
	m.ctxMax = 40000
	m.push(&block{kind: kUser, text: "open safari and search for otters"})

	for _, line := range script {
		m.fold(parse(t, line))
	}
	m.rebuild()
	return m
}

var ansiRe = regexp.MustCompile(`\x1b\[[0-9;]*m`)

func plainOf(s string) string { return ansiRe.ReplaceAllString(s, "") }

// TestFrameWidth is the layout invariant: no row of any frame is wider than the
// terminal, at every width, in every toggle state. A row one cell too wide is a
// box that no longer closes.
//
// Rows are allowed to stop short of the right edge — they are not padded out to
// it, so that selecting text in the terminal does not pick up a rectangle of
// trailing spaces. The status bar is the one row that must fill the width,
// because its blanks carry a background.
func TestFrameWidth(t *testing.T) {
	for _, w := range []int{20, 24, 28, 40, 60, 80, 100, 140, 200} {
		for _, flags := range []struct{ think, fold bool }{{false, false}, {true, false}, {false, true}, {true, true}} {
			m := play(t, w, 24)
			m.showThinking, m.foldCalls = flags.think, flags.fold
			m.rebuild()

			rows := strings.Split(m.render(), "\n")
			if len(rows) != m.height {
				t.Fatalf("w=%d think=%v fold=%v: got %d rows, want %d", w, flags.think, flags.fold, len(rows), m.height)
			}
			for i, row := range rows {
				if got := vw(row); got > w {
					t.Errorf("w=%d think=%v fold=%v row %d: width %d\n%q", w, flags.think, flags.fold, i, got, plainOf(row))
				}
			}
			if got := vw(m.renderStatus()); got != w {
				t.Errorf("w=%d think=%v fold=%v: status bar %d cells, want %d", w, flags.think, flags.fold, got, w)
			}
			// No row but the status bar carries trailing padding: a copied
			// paragraph should be the paragraph, not the paragraph and the
			// blank rectangle to the right of it.
			for i, row := range rows {
				// The bar's blanks are the bar, and the cursor is a blank in
				// reverse video. Both are allowed to end a row.
				if strings.Contains(row, barBG) || strings.Contains(row, reverse) {
					continue
				}
				if plain := plainOf(row); plain != strings.TrimRight(plain, " ") {
					t.Errorf("w=%d row %d ends in padding: %q", w, i, plain)
				}
			}
		}
	}
}

// TestStatusBarTrims is the bar's own rule: a wide terminal shows everything,
// a narrow one gives up readings in order of what they are worth, and the state
// is never one of them.
func TestStatusBarTrims(t *testing.T) {
	loaded := func(w int) *model {
		m := play(t, w, 24)
		m.provider, m.modelID = "anthropic", "claude-sonnet-4-20250514"
		m.effort, m.askMode, m.scroll = "max", false, 4
		m.lastRun = 90 * time.Second
		return m
	}

	// Everything on, and room for all of it.
	wide := plainOf(loaded(200).renderStatus())
	for _, want := range []string{"tools", "calls", "1m30s", "scrolled 4", "no prompts",
		"effort max", "anthropic", "claude-sonnet-4", "ctx ", "16k/40k", "msgs", "turns"} {
		if !strings.Contains(wide, want) {
			t.Errorf("wide bar is missing %q:\n%s", want, wide)
		}
	}
	// The date is not part of the name anyone reads.
	if strings.Contains(wide, "20250514") {
		t.Errorf("model id kept its release date:\n%s", wide)
	}

	// Narrow, and the state is the last thing standing.
	for _, w := range []int{20, 24, 30, 40, 60} {
		bar := plainOf(loaded(w).renderStatus())
		if !strings.Contains(bar, "tools") {
			t.Errorf("w=%d: state dropped from the bar: %q", w, bar)
		}
		// What is left is only ever a subset of what a wide bar shows, and the
		// cheap readings go before the dear ones.
		if strings.Contains(bar, "ctx ") && !strings.Contains(bar, "calls") {
			t.Errorf("w=%d: kept the gauge over the call count: %q", w, bar)
		}
	}
}

// TestGauge covers what the meter does with each of the answers it can get:
// both numbers, only what was spent, only what is left, and nothing at all.
func TestGauge(t *testing.T) {
	cases := []struct {
		name   string
		snap   session.Snapshot
		ctxMax int
		want   string
	}{
		{"used and window", session.Snapshot{ContextUsed: 24500, ContextMax: 200000}, 200000, "24k/200k 12%"},
		{"nearly full", session.Snapshot{ContextUsed: 178000, ContextMax: 200000}, 200000, "178k/200k 89%"},
		// No denominator anyone can vouch for: a count, and no meter drawn
		// against a number that was made up.
		{"no window", session.Snapshot{ContextUsed: 12000}, 0, "ctx 12k"},
		// The older contract: a worker that reports only what is left.
		{"left only", session.Snapshot{ContextLeft: 16000}, 40000, "24k/40k 60%"},
		{"nothing reported", session.Snapshot{}, 0, ""},
	}
	for _, c := range cases {
		m := initialModel()
		m.width, m.height = 120, 24
		m.status, m.ctxMax = c.snap, c.ctxMax

		got := plainOf(m.gauge())
		if c.want == "" {
			if got != "" {
				t.Errorf("%s: drew %q, want nothing", c.name, got)
			}
			continue
		}
		if !strings.Contains(got, c.want) {
			t.Errorf("%s: %q, want it to contain %q", c.name, got, c.want)
		}
	}
}

// TestTrimTrailing covers the two halves of the rule: padding leaves, however
// it was styled, and the bar's background blanks stay.
func TestTrimTrailing(t *testing.T) {
	cases := []struct{ name, in, want string }{
		{"plain tail", "  hello   ", "  hello"},
		{"inside a colour", paint(cMuted, "hi   "), cMuted + "hi" + styleOff},
		{"between two runs", paint(cInk, "a") + paint(cMuted, "  b  "), cInk + "a" + styleOff + cMuted + "  b" + styleOff},
		{"nothing but indent", "    ", ""},
		{"escapes only", cMuted + styleOff, cMuted + styleOff},
		{"background kept", barBG + "x   " + reset, barBG + "x   " + reset},
		{"cursor kept", "hi" + reverse + " " + revOff, "hi" + reverse + " " + revOff},
		{"pad after the cursor", "hi" + reverse + " " + revOff + "   ", "hi" + reverse + " " + revOff},
		// Typed spaces in front of the cursor are neither trailing nor the
		// cursor's, and must not be moved into its reverse video.
		{
			"spaces before the cursor",
			paint(cInk, "hi  ") + reverse + " " + revOff,
			paint(cInk, "hi  ") + reverse + " " + revOff,
		},
	}
	for _, c := range cases {
		if got := trimTrailing(c.in); got != c.want {
			t.Errorf("%s: %q, want %q", c.name, got, c.want)
		}
	}
}

// TestCursorInFrame is the regression: the cursor is the last cell of the input
// row, and trimming the row's padding must not take it with it.
func TestCursorInFrame(t *testing.T) {
	// Trailing spaces in the buffer are the case that broke: they sit between
	// the text and the cursor, so trimming must neither drop them nor let them
	// drift into the cursor's reverse video and paint blocks of their own.
	for _, typed := range []string{"typing", "typing  "} {
		m := play(t, 80, 24)
		for _, r := range typed {
			m.insert(r)
		}
		m.rebuild()

		var b strings.Builder
		m.row(&b, m.renderInput()[0])
		row := b.String()

		if !strings.Contains(row, reverse+" "+revOff) {
			t.Fatalf("%q: cursor missing from the input row: %q", typed, row)
		}
		if got := strings.Count(row, reverse); got != 1 {
			t.Errorf("%q: %d cursor blocks in the input row: %q", typed, got, row)
		}
		if plain := plainOf(row); !strings.HasSuffix(plain, typed+" ") {
			t.Errorf("%q: input row ends %q, want the cursor cell after the text", typed, plain)
		}
	}
}

// TestScrollClamped walks the viewport over the whole feed and past both ends.
func TestScrollClamped(t *testing.T) {
	m := play(t, 80, 12)
	for _, n := range []int{-100, 1, 5, 100, -3, -100} {
		m.scrollBy(n)
		if m.scroll < 0 || m.scroll > m.maxScroll() {
			t.Fatalf("scroll %d out of [0,%d]", m.scroll, m.maxScroll())
		}
		rows := strings.Split(m.render(), "\n")
		// The height invariant matters most here: a scrolled frame one row too
		// tall scrolls the terminal, and a terminal that keeps scrollback for
		// the alternate screen keeps every row it pushes off the top.
		if len(rows) != m.height {
			t.Fatalf("scroll %d: got %d rows, want %d", m.scroll, len(rows), m.height)
		}
		for _, row := range rows {
			if vw(row) > m.width {
				t.Fatalf("scroll %d: bad row %q", m.scroll, plainOf(row))
			}
		}
	}
}

// TestCallBatches checks the boundary rule: the worker marks no batch on the
// wire, so a batch has to close when prose resumes after a result.
func TestCallBatches(t *testing.T) {
	m := play(t, 80, 24)

	var batches []*block
	for _, b := range m.blocks {
		if b.kind == kCalls {
			batches = append(batches, b)
		}
	}
	if len(batches) != 2 {
		t.Fatalf("got %d batches, want 2", len(batches))
	}
	if m.calls != nil {
		t.Error("batch still open after done")
	}
	if m.callCount != 5 {
		t.Errorf("counted %d tool calls, want 5", m.callCount)
	}

	want := [][]string{{"app_open", "screenshot", "click"}, {"type_text", "key"}}
	for i, b := range batches {
		if b.open {
			t.Errorf("batch %d still open", i+1)
		}
		if len(b.acts) != len(want[i]) {
			t.Fatalf("batch %d: %d calls, want %d", i+1, len(b.acts), len(want[i]))
		}
		for j, a := range b.acts {
			if a.name != want[i][j] {
				t.Errorf("batch %d call %d: %q, want %q", i+1, j, a.name, want[i][j])
			}
			if a.state == actPending {
				t.Errorf("batch %d call %q never settled", i+1, a.name)
			}
		}
	}

	failed := batches[0].acts[2]
	if failed.state != actFail {
		t.Errorf("failed click recorded as %v (%q)", failed.state, failed.res)
	}
	if !strings.Contains(plainOf(m.render()), "screen coordinates out of range") {
		t.Error("failure detail missing from the frame")
	}
	if got := batches[0].acts[1].res; got != "1 img" {
		t.Errorf("screenshot result %q, want %q", got, "1 img")
	}
}

// TestParseCalls covers the three provider dialects that reach the wire.
func TestParseCalls(t *testing.T) {
	cases := []struct {
		name, raw, wantName, wantArg string
	}{
		{"ollama", `[{"function":{"name":"click","arguments":{"x":10,"y":20,"button":"right"}}}]`, "click", "(10, 20) right"},
		{"openai", `[{"id":"c1","type":"function","function":{"name":"key","arguments":"{\"combo\":\"cmd+l\"}"}}]`, "key", "cmd+l"},
		{"anthropic", `[{"type":"tool_use","id":"c1","name":"wait","input":{"seconds":1.5}}]`, "wait", "1.5s"},
		{"scroll", `[{"function":{"name":"scroll","arguments":{"x":5,"y":6,"dy":-3}}}]`, "scroll", "(5, 6) down 3"},
		{"unknown tool", `[{"function":{"name":"weird","arguments":{"b":2,"a":1}}}]`, "weird", "a=1 b=2"},
	}
	for _, c := range cases {
		got := parseCalls(c.raw)
		if len(got) != 1 {
			t.Fatalf("%s: %d calls, want 1", c.name, len(got))
		}
		if got[0].name != c.wantName || got[0].arg != c.wantArg {
			t.Errorf("%s: got %q %q, want %q %q", c.name, got[0].name, got[0].arg, c.wantName, c.wantArg)
		}
	}

	// A payload in no known shape still has to survive as something visible.
	if got := parseCalls(`{"not":"an array"}`); len(got) != 1 || got[0].arg == "" {
		t.Errorf("unparseable payload dropped: %+v", got)
	}
	if got := parseCalls(``); got != nil {
		t.Errorf("empty payload produced %+v", got)
	}
}

// TestNoControlChars makes sure nothing from the wire can desync the layout:
// the only escape sequences in a frame are the ones the palette put there.
func TestNoControlChars(t *testing.T) {
	m := play(t, 80, 24)
	m.fold(parse(t, `{"type":"token","id":"x","data":{"state":"tool_calls","token":[{"function":{"name":"type_text","arguments":{"text":"a\nb\tcd[31m"}}}],"status":"tooling"}}`))
	m.rebuild()

	for _, row := range strings.Split(m.render(), "\n") {
		for _, r := range plainOf(row) {
			if r < 0x20 || r == 0x7f {
				t.Fatalf("control char %q in row %q", r, plainOf(row))
			}
		}
	}
}

// TestKeys drives the model the way the program does — real messages through
// Update — so the bindings are checked against the key codes bubbletea
// actually delivers, not against the ones the switch happens to name.
func TestKeys(t *testing.T) {
	m := initialModel()
	m.sess = session.New(nil, session.Options{}) // never started: SendChat just errors
	step := func(msg tea.Msg) { m.Update(msg) }

	step(tea.WindowSizeMsg{Width: 96, Height: 28})
	for _, line := range script {
		step(parse(t, line))
	}

	// The whole feed, not just the visible window — these assertions are about
	// what a toggle produced, not about where the viewport happens to sit.
	frame := func() string { return plainOf(strings.Join(m.wrapped, "\n")) }

	if !strings.Contains(frame(), "⋮ thinking") {
		t.Fatal("thinking not collapsed by default")
	}
	step(tea.KeyPressMsg{Code: 't', Mod: tea.ModCtrl}) // ctrl+t
	if !m.showThinking || strings.Contains(frame(), "⋮ thinking ") {
		t.Error("ctrl+t did not expand thinking")
	}

	// Expanded, each call has its own row; collapsed, the names join the header.
	const rolled = "app_open, screenshot, click"
	if strings.Contains(frame(), rolled) {
		t.Fatal("tool calls collapsed by default")
	}
	step(tea.KeyPressMsg{Code: tea.KeyTab})
	if !m.foldCalls || !strings.Contains(frame(), rolled) {
		t.Error("tab did not collapse the tool calls")
	}
	step(tea.KeyPressMsg{Code: tea.KeyTab})
	if m.foldCalls || strings.Contains(frame(), rolled) {
		t.Error("tab did not expand the tool calls again")
	}

	// Type, correct a word, and send.
	for _, r := range "otter search" {
		step(tea.KeyPressMsg{Code: r, Text: string(r)})
	}
	step(tea.KeyPressMsg{Code: 'w', Mod: tea.ModCtrl})
	for _, r := range "hunt" {
		step(tea.KeyPressMsg{Code: r, Text: string(r)})
	}
	if got := string(m.input); got != "otter hunt" {
		t.Errorf("input %q, want %q", got, "otter hunt")
	}

	step(tea.KeyPressMsg{Code: tea.KeyEnter})
	if len(m.input) != 0 || m.cursor != 0 {
		t.Errorf("input not cleared on send: %q", string(m.input))
	}
	if last := m.blocks[len(m.blocks)-1]; last.kind != kUser || last.text != "otter hunt" {
		t.Errorf("sent message did not land in the feed: %+v", last)
	}

	// Scrolling stops at both ends and the frame stays whole throughout.
	for _, msg := range []tea.Msg{
		tea.KeyPressMsg{Code: tea.KeyPgUp}, tea.KeyPressMsg{Code: tea.KeyUp},
		tea.MouseWheelMsg{Button: tea.MouseWheelUp}, tea.MouseWheelMsg{Button: tea.MouseWheelDown},
		tea.KeyPressMsg{Code: tea.KeyPgDown}, spinTickMsg{ID: m.spinID},
	} {
		step(msg)
		for _, row := range strings.Split(m.render(), "\n") {
			if vw(row) > m.width {
				t.Fatalf("%T left a %d-cell row: %q", msg, vw(row), plainOf(row))
			}
		}
	}
}

// TestFormatting checks the one property the formatter must never break: the
// markers are gone from the text, the styling is real, and no line is wider
// than the width it was given — at every width, including absurd ones.
func TestFormatting(t *testing.T) {
	for _, w := range []int{4, 8, 12, 20, 40, 78} {
		for _, line := range formatProse(mdProse, w) {
			if got := spanWidth(line); got > w {
				t.Errorf("w=%d: line of %d cells: %q", w, got, plainSpans(line))
			}
			if txt := plainSpans(line); strings.Contains(txt, "**") || strings.Contains(txt, "```") {
				t.Errorf("w=%d: markers survived formatting: %q", w, txt)
			}
		}
	}

	styled := formatProse("plain **bold** `code` *slanted*", 78)
	if len(styled) != 1 {
		t.Fatalf("got %d lines, want 1", len(styled))
	}
	want := map[string]string{"bold": sBold, "code": sCode, "slanted": sItalic, "plain": ""}
	for _, sp := range styled[0] {
		if style, ok := want[sp.text]; ok && sp.style != style {
			t.Errorf("%q styled %q, want %q", sp.text, sp.style, style)
		}
	}

	// Emphasis half-typed by a model still streaming must stay literal rather
	// than restyling the rest of the answer.
	for _, s := range []string{"an **unclosed bold", "a `dangling code", "*"} {
		if got := plainSpans(formatProse(s, 78)[0]); got != s {
			t.Errorf("unclosed marker rewritten: %q -> %q", s, got)
		}
	}
}

// TestShimmer checks the two things that make an animation look smooth rather
// than laggy: it is driven by the clock, so it advances by the same amount for
// the same elapsed time however often it is sampled, and it never has a phase
// where the band is missing entirely.
func TestShimmer(t *testing.T) {
	line := []span{{text: strings.Repeat("x", 60)}}

	lit := func(d time.Duration) (first, count int) {
		first = -1
		for i, sp := range shimmerSpans(line, d) {
			if sp.style != "" {
				if first < 0 {
					first = i
				}
				count++
			}
		}
		return first, count
	}

	// Sampling at 20fps and at 3fps must put the band in the same place at the
	// same instant: nothing about the effect depends on the repaint rate.
	for ms := 0; ms < 2000; ms += 50 {
		d := time.Duration(ms) * time.Millisecond
		a, _ := lit(d)
		b, _ := lit(d)
		if a != b {
			t.Fatalf("%v: not a pure function of time (%d vs %d)", d, a, b)
		}
	}

	// Over a full cycle the band is always at least partly on screen, and the
	// line never changes width.
	dark := 0
	for ms := 0; ms < 4000; ms += 25 {
		d := time.Duration(ms) * time.Millisecond
		painted := shimmerSpans(line, d)
		if got := spanWidth(painted); got != 60 {
			t.Fatalf("%v: shimmer changed the width to %d", d, got)
		}
		if _, n := lit(d); n == 0 {
			dark++
		}
	}
	if dark > 0 {
		t.Errorf("band was entirely off screen in %d sampled frames", dark)
	}

	// And it actually moves.
	if a, _ := lit(0); a == func() int { b, _ := lit(300 * time.Millisecond); return b }() {
		t.Error("band did not advance over 300ms")
	}
}

func plainSpans(line []span) string {
	var b strings.Builder
	for _, sp := range line {
		b.WriteString(sp.text)
	}
	return b.String()
}

// TestPaste checks that a pasted block lands in the input as one line: a
// newline reaching the key handler would submit half of it.
func TestPaste(t *testing.T) {
	m := initialModel()
	m.width, m.height = 80, 24
	m.Update(tea.PasteMsg{Content: "open\nsafari  and\tsearch"})

	if got := string(m.input); got != "open safari and search" {
		t.Errorf("pasted %q, want %q", got, "open safari and search")
	}
	if m.cursor != len(m.input) {
		t.Errorf("cursor at %d, want %d", m.cursor, len(m.input))
	}

	// Pasting mid-line inserts at the cursor rather than appending.
	m.cursor = 4
	m.Update(tea.PasteMsg{Content: " a"})
	if got := string(m.input); got != "open a safari and search" {
		t.Errorf("mid-line paste gave %q", got)
	}
}

// TestMultilineInput covers shift/alt+enter: a newline goes into the message
// rather than sending it, the input grows, and the frame stays whole.
func TestMultilineInput(t *testing.T) {
	m := play(t, 80, 24)
	m.sess = session.New(nil, session.Options{})

	type key = tea.KeyPressMsg
	newline := key{Code: tea.KeyEnter, Mod: tea.ModShift}

	before := m.contentHeight()
	for _, r := range "first" {
		m.Update(key{Code: r, Text: string(r)})
	}
	m.Update(newline)
	for _, r := range "second" {
		m.Update(key{Code: r, Text: string(r)})
	}

	if got := string(m.input); got != "first\nsecond" {
		t.Fatalf("input %q, want %q", got, "first\nsecond")
	}
	if m.inputHeight() != 2 {
		t.Errorf("input is %d rows, want 2", m.inputHeight())
	}
	if m.contentHeight() != before-1 {
		t.Errorf("feed did not give up a row for the second input line")
	}

	// Alt+Enter does the same, for terminals that do not report shift on enter.
	m.Update(key{Code: tea.KeyEnter, Mod: tea.ModAlt})
	if got := string(m.input); got != "first\nsecond\n" {
		t.Errorf("alt+enter gave %q", got)
	}

	// Up and down walk the input lines rather than scrolling the feed.
	m.cursor = len(m.input)
	if !m.moveLine(-1) || m.moveLine(-1) != true {
		t.Error("up did not move between input lines")
	}
	if row := func() int { _, r, _ := m.inputLines(); return r }(); row != 0 {
		t.Errorf("cursor on row %d after two ups, want 0", row)
	}
	if m.moveLine(-1) {
		t.Error("up moved past the first line")
	}

	for _, row := range strings.Split(m.render(), "\n") {
		if vw(row) > m.width {
			t.Fatalf("multi-line input broke the frame: %q", plainOf(row))
		}
	}

	// Enter still sends, and sends the whole thing.
	m.Update(key{Code: tea.KeyEnter})
	if len(m.input) != 0 {
		t.Errorf("enter did not send: %q", string(m.input))
	}
	if last := m.blocks[len(m.blocks)-1]; last.kind != kUser || !strings.Contains(last.text, "\n") {
		t.Errorf("the newline did not survive being sent: %+v", last)
	}
}

// TestFrame prints frames for eyeballing: go test -run TestFrame -v
func TestFrame(t *testing.T) {
	for _, c := range []struct {
		name        string
		think, fold bool
		scroll      int
	}{
		{name: "bottom"},
		{name: "top", scroll: 100},
		{name: "thinking expanded", think: true},
		{name: "tool calls collapsed", fold: true},
	} {
		m := play(t, 88, 26)
		m.showThinking, m.foldCalls = c.think, c.fold
		m.rebuild()
		m.scrollBy(c.scroll)
		t.Logf("[%s]\n%s", c.name, plainOf(m.render()))
	}
}
