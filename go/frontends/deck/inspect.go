package main

// The inspector: one tool call, in full.
//
// The feed is an action tape — a call is a row, and a row is a name, a short
// argument and a result word. That is the right density for watching a run and
// the wrong one for answering "what did it actually send, and what came back".
// This is the other half: the same call, opened.
//
// It reads from two places, because the two halves of a call arrive
// differently. The arguments cross the wire in full on the tool_calls event and
// are already held in the block model, so they are on screen the moment the
// inspector opens — including for the call running right now, which is exactly
// when you want to see them. The result does not cross the wire: the worker
// sends a size where a build log or a web page would be, on purpose, so it is
// asked for by index (tool.detail) and filled in when it lands.
//
// Nothing here draws a box or a JSON dump. Same left margin, same text column
// and the same quiet palette as the feed: the key column is the only structure,
// and anything too long for a line becomes a gutter block underneath it.

import (
	"encoding/json"
	"sort"
	"strings"
)

// callRef locates one call in the feed, so the inspector can step through them
// in the order they happened without holding pointers into a slice that grows
// underneath it.
type callRef struct {
	b *block
	i int
}

func (r callRef) act() act { return r.b.acts[r.i] }

// detail is a worker answer, or the fact that it has been asked for.
type detail struct {
	pending     bool
	unavailable string
	args        json.RawMessage
	result      json.RawMessage
}

type inspector struct {
	on     bool
	at     int // index into the call list, as of the last rebuild
	scroll int

	rows  []string
	rowsW int

	// Answers by tool-record index, kept for the session: reopening a call
	// already read costs nothing, and the worker is not asked twice.
	got map[int]*detail
}

// open puts the inspector up on the most recent call, which is the one the
// question is nearly always about.
func (m *model) openInspector() {
	calls := m.callList()
	if len(calls) == 0 {
		m.notice(cGhost, "no tool calls to inspect yet")
		return
	}
	if m.insp.got == nil {
		m.insp.got = map[int]*detail{}
	}
	m.insp.on, m.insp.at, m.insp.scroll, m.insp.rows = true, len(calls)-1, 0, nil
	m.fetchDetail(calls[m.insp.at].act())
}

func (m *model) closeInspector() {
	m.insp.on = false
	m.insp.rows = nil
}

// stepInspector walks to another call, holding at the ends rather than wrapping
// — a list you can fall off the end of is a list you lose your place in.
func (m *model) stepInspector(d int) {
	calls := m.callList()
	if len(calls) == 0 {
		m.closeInspector()
		return
	}
	at := min(max(m.insp.at+d, 0), len(calls)-1)
	if at == m.insp.at {
		return
	}
	m.insp.at, m.insp.scroll, m.insp.rows = at, 0, nil
	m.fetchDetail(calls[at].act())
}

// callList flattens every call in the feed, in the order they were made.
func (m *model) callList() []callRef {
	var out []callRef
	for _, b := range m.blocks {
		if b.kind != kCalls {
			continue
		}
		for i := range b.acts {
			out = append(out, callRef{b: b, i: i})
		}
	}
	return out
}

// fetchDetail asks the worker for a call's stored record, once. A call still in
// flight has no record yet, so there is nothing to ask for and the arguments
// already on screen are the whole answer until it settles.
func (m *model) fetchDetail(a act) {
	if a.index < 0 || m.insp.got[a.index] != nil {
		return
	}
	m.insp.got[a.index] = &detail{pending: true}
	m.command("tool.detail", map[string]any{"index": a.index})
}

// refreshInspector asks for the open call's record as soon as it has one. The
// inspector is worth opening on a call that is still running — the arguments
// are there from the start — and this is what fills the other half in when it
// lands, without the user having to leave the page and come back.
func (m *model) refreshInspector() {
	if !m.insp.on {
		return
	}
	calls := m.callList()
	if m.insp.at >= len(calls) {
		return
	}
	if a := calls[m.insp.at].act(); a.index >= 0 && m.insp.got[a.index] == nil {
		m.insp.rows = nil
		m.fetchDetail(a)
	}
}

// waiting reports whether the open call has something on its way — a result, or
// the record of one. The animation clock runs while it does, so the spinner on
// the page is not left frozen mid-turn by an otherwise idle screen.
func (m *model) inspectWaiting() bool {
	if !m.insp.on {
		return false
	}
	calls := m.callList()
	if m.insp.at >= len(calls) {
		return false
	}
	return m.inspectLive(calls[m.insp.at].act())
}

// takeDetail files a worker answer. It arrives whether or not the inspector is
// still on the call that asked for it — the answer is kept either way, because
// the next visit to that call should not have to ask again.
func (m *model) takeDetail(data json.RawMessage) {
	var reply struct {
		Index       *int            `json:"index"`
		Name        string          `json:"name"`
		Unavailable string          `json:"unavailable"`
		Args        json.RawMessage `json:"args"`
		Result      json.RawMessage `json:"result"`
	}
	if json.Unmarshal(data, &reply) != nil || reply.Index == nil {
		return
	}
	if m.insp.got == nil {
		m.insp.got = map[int]*detail{}
	}
	m.insp.got[*reply.Index] = &detail{
		unavailable: reply.Unavailable,
		args:        reply.Args,
		result:      reply.Result,
	}
	m.insp.rows = nil
}

// ---------------------------------------------------------------------------
// rendering

// keyCol is the width of the field-name column. Wide enough for the names the
// toolbox actually uses (command, exit_code, image_base64) to be read whole,
// without pushing the values into the far half of the screen.
const keyCol = 14

// inspectRows renders the open call, caching until something changes under it:
// a new width, a different call, or the answer arriving.
func (m *model) inspectRows() []string {
	if m.insp.rows != nil && m.insp.rowsW == m.width {
		return m.insp.rows
	}

	calls := m.callList()
	if len(calls) == 0 {
		return []string{margin + paint(cGhost, "nothing to inspect")}
	}
	m.insp.at = min(m.insp.at, len(calls)-1)

	a := calls[m.insp.at].act()
	width := m.bodyW()
	rows := []string{m.inspectHead(a, len(calls), width), ""}

	// Arguments first, and never from the worker: they are already here, in
	// full, from the moment the call was made.
	rows = append(rows, m.section("arguments", width))
	switch args := m.localArgs(a); {
	case args != nil:
		rows = append(rows, valueRows(args, width, 0)...)
	case strings.TrimSpace(a.args) != "":
		// Arguments that would not decode are still what was sent, so they are
		// shown as they arrived rather than reported as nothing.
		rows = append(rows, blockRows(plain(a.args), width, 1)...)
	default:
		rows = append(rows, margin+"  "+paint(cGhost, "none"))
	}

	rows = append(rows, "", m.section("result", width))
	rows = append(rows, m.resultRows(a, width)...)

	if a.note != "" {
		rows = append(rows, "", m.section("error", width))
		for _, line := range wrapPlain(a.note, width-2) {
			rows = append(rows, margin+"  "+paint(cErr, line))
		}
	}

	// A call still running, or an answer still on its way, has a spinner in it.
	// Caching that would freeze the one thing on the page that moves.
	if !m.inspectLive(a) {
		m.insp.rows, m.insp.rowsW = rows, m.width
	}
	return rows
}

// inspectLive reports whether the open call is still waiting on something.
func (m *model) inspectLive(a act) bool {
	if a.state == actPending || a.index < 0 {
		return a.state == actPending
	}
	d := m.insp.got[a.index]
	return d != nil && d.pending
}

// inspectHead is the one line that says which call this is: the same marker,
// tone and result column the feed uses for it, so the row you opened and the
// page you are looking at read as the same thing.
func (m *model) inspectHead(a act, total, width int) string {
	pos := "call " + itoa(m.insp.at+1) + " of " + itoa(total)
	head := margin + paint(cRule, "▸ ") + paint(toolColor(a.name), a.name) + paint(cGhost, sep+pos)

	res := m.resText(a)
	if gap := 2 + width - vw(head) - vw(res); gap > 1 {
		head += strings.Repeat(" ", gap) + paint(resColor(a), res)
	}
	return head
}

// section is a field group's label, under a rule that runs to the measure. The
// rule is what keeps two groups of key/value rows from reading as one.
func (m *model) section(label string, width int) string {
	row := margin + paint(cCall, label)
	if fill := width - vw(label) - 2; fill > 1 {
		row += paint(cRule, " "+strings.Repeat("─", fill))
	}
	return row
}

// localArgs decodes the arguments the call was made with, from the tool_calls
// payload the feed already holds.
func (m *model) localArgs(a act) map[string]any {
	if a.args == "" {
		return nil
	}
	args := decodeArgs(json.RawMessage(a.args))
	if len(args) == 0 {
		return nil
	}
	return args
}

// resultRows is the half that has to come from the worker: what a call actually
// returned, rather than the word the feed had room for.
func (m *model) resultRows(a act, width int) []string {
	indent := margin + "  "
	switch {
	case a.state == actPending:
		return []string{indent + paint(cCall, m.spinGlyph()+" still running")}
	case a.index < 0:
		return []string{indent + paint(cGhost, "no record for this call")}
	}

	d := m.insp.got[a.index]
	switch {
	case d == nil:
		return []string{indent + paint(cGhost, "not loaded")}
	case d.pending:
		return []string{indent + paint(cCall, m.spinGlyph()+" loading")}
	case d.unavailable != "":
		return []string{indent + paint(cGhost, d.unavailable)}
	}

	var v any
	if json.Unmarshal(d.result, &v) != nil {
		return []string{indent + paint(cGhost, "unreadable result")}
	}
	// Every tool answers as {"result": ...} or {"error": ...}; the wrapper is
	// noise on a page that already says which call this is and whether it
	// worked, so it is unwrapped and the inside is what gets drawn.
	if wrapper, is := v.(map[string]any); is && len(wrapper) == 1 {
		for _, k := range []string{"result", "error"} {
			if inner, found := wrapper[k]; found {
				v = inner
			}
		}
	}
	rows := valueRows(v, width, 0)
	if len(rows) == 0 {
		return []string{indent + paint(cGhost, "empty")}
	}
	return rows
}

// valueRows lays a decoded JSON value out as rows. Objects become a key column
// with values beside it; anything too long or too structured for one line goes
// underneath, indented or in a gutter block. Depth is the nesting level, which
// is the only thing that moves the text column.
func valueRows(v any, width, depth int) []string {
	pad := margin + strings.Repeat("  ", depth+1)
	avail := width - vw(pad) + vw(margin)

	switch t := v.(type) {
	case map[string]any:
		keys := make([]string, 0, len(t))
		for k := range t {
			keys = append(keys, k)
		}
		sort.Strings(keys)

		var rows []string
		for _, k := range keys {
			rows = append(rows, fieldRows(k, t[k], width, depth)...)
		}
		return rows

	case []any:
		var rows []string
		for _, item := range t {
			// A list of scalars stays a list of rows; a list of objects gets its
			// members indented under a bullet so two of them cannot blur into
			// one.
			if inner := valueRows(item, width, depth+1); len(inner) == 1 {
				rows = append(rows, pad+paint(cRule, "• ")+strings.TrimLeft(inner[0], " "))
			} else {
				rows = append(rows, pad+paint(cRule, "•"))
				rows = append(rows, inner...)
			}
		}
		return rows

	default:
		return []string{pad + paint(cInk, trunc(scalar(v), max(avail, 8)))}
	}
}

// fieldRows draws one key and its value.
func fieldRows(key string, v any, width, depth int) []string {
	pad := margin + strings.Repeat("  ", depth+1)
	label := paint(cMuted, padTo(trunc(key, keyCol-1), keyCol))
	avail := width - vw(pad) - keyCol + vw(margin)

	switch t := v.(type) {
	case map[string]any:
		// A blob ref is a stored image, not a nested object worth unfolding.
		if ref, is := blobRef(t); is {
			return []string{pad + label + paint(cLook, ref)}
		}
		if len(t) == 0 {
			return []string{pad + label + paint(cGhost, "{}")}
		}
		return append([]string{pad + label}, valueRows(t, width, depth+1)...)

	case []any:
		if len(t) == 0 {
			return []string{pad + label + paint(cGhost, "[]")}
		}
		return append([]string{pad + label}, valueRows(t, width, depth+1)...)

	case string:
		// The whole reason this view exists: a command's output, a page, a
		// prompt. One line if it fits on one, a gutter block if it does not.
		s := plain(t)
		if !strings.Contains(s, "\n") && vw(s) <= avail {
			return []string{pad + label + paint(cInk, s)}
		}
		return append([]string{pad + label}, blockRows(s, width, depth+1)...)

	default:
		return []string{pad + label + paint(cInk, trunc(scalar(v), max(avail, 8)))}
	}
}

// blockRows draws a long or multi-line string under a gutter, the way a quote
// is drawn in prose: the bar is what tells you where the text you are reading
// starts and ends when it is forty lines of somebody else's stdout.
func blockRows(s string, width, depth int) []string {
	pad := margin + strings.Repeat("  ", depth)
	inner := max(width-vw(pad)-2+vw(margin), 8)

	var rows []string
	for _, line := range wrapPlain(strings.TrimRight(s, "\n"), inner) {
		rows = append(rows, pad+paint(cRule, "│ ")+paint(cInk, line))
	}
	return rows
}

// plain makes stored text safe to lay out. Tabs become spaces — a tab measures
// no cells at all, so one left in place puts every column after it somewhere
// the layout did not account for — and control characters go. Newlines stay:
// they are what makes a block a block.
func plain(s string) string {
	var b strings.Builder
	b.Grow(len(s))
	for _, r := range s {
		switch {
		case r == '\n':
			b.WriteByte('\n')
		case r == '\t':
			b.WriteString("  ")
		case r < 0x20 || r == 0x7f:
			// dropped
		default:
			b.WriteRune(r)
		}
	}
	return b.String()
}

// blobRef names a stored image by the ref the session split it out as, so the
// row says an image is there rather than printing a megabyte of base64 at you.
func blobRef(m map[string]any) (string, bool) {
	if len(m) != 1 {
		return "", false
	}
	digest, is := m["$blob"].(string)
	if !is {
		return "", false
	}
	if len(digest) > 8 {
		digest = digest[:8]
	}
	return "image · " + digest, true
}

// inspectKeys replaces the input row while the inspector is up. The prompt is
// gone because the keys are the inspector's: leaving a cursor blinking under a
// page that swallows every keystroke is how a UI teaches you it is broken.
func (m *model) inspectKeys() []string {
	keys := []string{"↑↓ scroll", "←→ call", "esc close"}
	return []string{margin + paint(cGhost, strings.Join(keys, sep))}
}
