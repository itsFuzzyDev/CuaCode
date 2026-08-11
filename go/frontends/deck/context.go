package main

// /context: what the window is full of.
//
// The worker does the counting and says which of its numbers were measured and
// which estimated; this only draws them. The drawing is a grid of cells against
// the model's window, because the question is a proportion — how much room is
// left, and what is taking the rest — and a proportion reads faster as an area
// than as a column of numbers. The numbers are beside it for when the area is
// not enough.

import (
	"encoding/json"
	"fmt"
	"strings"
	"time"
)

type ctxPart struct {
	Key    string `json:"key"`
	Label  string `json:"label"`
	Tokens int    `json:"tokens"`
	Count  int    `json:"count"`
}

type ctxSection struct {
	Key    string `json:"key"`
	Count  int    `json:"count"`
	Noun   string `json:"noun"`
	Tokens int    `json:"tokens"`
	Hint   string `json:"hint"`
}

// ctxTurn is what the last round that generated actually cost. Kept apart from
// the window breakdown: one is the state of the conversation, the other is how
// the model behaved a minute ago.
type ctxTurn struct {
	In       int     `json:"input"`
	Out      int     `json:"out_tokens"`
	Thinking int     `json:"thinking_tokens"`
	Reply    int     `json:"reply_tokens"`
	Est      bool    `json:"thinking_est"`
	ReplyEst bool    `json:"reply_est"`
	TPS      float64 `json:"tps"`
	ThinkTPS float64 `json:"think_tps"`
	ReplyTPS float64 `json:"reply_tps"`
	Secs     float64 `json:"gen_secs"`
	ThinkSec float64 `json:"thinking_secs"`
	ReplySec float64 `json:"reply_secs"`
}

type ctxReport struct {
	Provider   string       `json:"provider"`
	Model      string       `json:"model"`
	Window     int          `json:"window"`
	Used       int          `json:"used"`
	Free       int          `json:"free"`
	Measured   int          `json:"measured"`
	Calibrated bool         `json:"calibrated"`
	Parts      []ctxPart    `json:"parts"`
	Sections   []ctxSection `json:"sections"`
	Last       ctxTurn      `json:"last"`
}

// takeContext turns a worker reply into a block in the feed. It goes in the feed
// rather than into an overlay because it is a reading, not a choice: you want it
// to still be there after you have typed the next thing.
func (m *model) takeContext(data json.RawMessage) {
	var rep ctxReport
	if json.Unmarshal(data, &rep) != nil {
		m.notice(cGhost, "could not read the context report")
		return
	}
	m.boundary()
	m.push(&block{kind: kContext, ctx: &rep})
}

// The palette, by category. Reusing the tool colours on purpose: the toolbox is
// the same colour here as it is in every batch of calls above.
var ctxTone = map[string]string{
	"system":      cLook,
	"environment": cSide,
	"tools":       cDrive,
	"skills":      cCall,
	"memory":      cUser,
	"mcp":         cWarn,
	"messages":    cOK,
	"images":      cCallLit,
}

func ctxColor(key string) string {
	if tone, ok := ctxTone[key]; ok {
		return tone
	}
	return cMuted
}

// The grid. Two cells per column so the squares read as squares rather than as a
// solid bar, and eight rows because that is a shape you can take in at a glance.
const (
	ctxCols  = 16
	ctxRows  = 8
	ctxCells = ctxCols * ctxRows
	// What the legend needs beside the grid to read without truncating its own
	// rows. With less than that the two stack instead.
	ctxLegend = 38
)

// gridShape fits the grid to the room there is: the full sixteen columns where
// they fit, fewer where they do not. Losing columns costs resolution — each cell
// stands for a larger share — and losing the grid entirely would cost the one
// part of this page that answers the question at a glance, so it shrinks rather
// than going.
func gridShape(avail int) (cols, rows, cells int) {
	cols = min(ctxCols, (avail+1)/2)
	cols = max(cols, 4)
	return cols, ctxRows, cols * ctxRows
}

// allocate hands out grid cells in proportion to what each part costs, with a
// floor of one cell for anything present at all. The floor is the point: a
// category worth 0.3% of a large window rounds to nothing, and a breakdown that
// silently omits half its own rows is worse than one that overstates a sliver.
func allocate(parts []ctxPart, total, cells int) []int {
	out := make([]int, len(parts))
	if total <= 0 || cells <= 0 {
		return out
	}

	// Rounded, never topped up: whatever is not claimed is free space, and a
	// grid that redistributed the leftovers would draw a full window every time.
	used := 0
	for i, p := range parts {
		if p.Tokens <= 0 {
			continue
		}
		n := max(int(float64(p.Tokens)/float64(total)*float64(cells)+0.5), 1)
		out[i] = n
		used += n
	}
	// Overshoot is possible once every present category has been given its
	// floor: take it back off the largest, which is the one that can least
	// notice losing a cell.
	for used > cells {
		big := -1
		for i, n := range out {
			if n > 1 && (big < 0 || n > out[big]) {
				big = i
			}
		}
		if big < 0 {
			break
		}
		out[big]--
		used--
	}
	return out
}

// pct renders a share of the window. Small shares keep a decimal, because "0%"
// next to four figures of tokens reads as a bug.
func pct(n, total int) string {
	if total <= 0 {
		return ""
	}
	f := float64(n) / float64(total) * 100
	switch {
	case f >= 10:
		return fmt.Sprintf("%.0f%%", f)
	case f >= 1:
		return fmt.Sprintf("%.1f%%", f)
	}
	return fmt.Sprintf("%.2f%%", f)
}

// gridRows lays the allocated cells out row by row, in category order, with
// whatever is left drawn as free space.
func (m *model) gridRows(rep *ctxReport, counts []int, cols, rows int) []string {
	cells := cols * rows
	glyphs := make([]string, 0, cells)
	for i, n := range counts {
		tone := ctxColor(rep.Parts[i].Key)
		for range n {
			glyphs = append(glyphs, paint(tone, "█"))
		}
	}
	for len(glyphs) < cells {
		glyphs = append(glyphs, paint(cGhost, "░"))
	}

	out := make([]string, 0, rows)
	for r := range rows {
		var b strings.Builder
		for c := range cols {
			if c > 0 {
				b.WriteByte(' ')
			}
			b.WriteString(glyphs[r*cols+c])
		}
		out = append(out, b.String())
	}
	return out
}

// legendRows is the reading beside the grid: who is answering, how much of the
// window is gone, and where it went.
func (m *model) legendRows(rep *ctxReport, width int) []string {
	total := rep.Window
	if total <= 0 {
		total = rep.Used // no window to measure against; shares are of what is spent
	}

	rows := []string{
		paint(cInk+bold, trunc(shortModel(rep.Model), width)),
	}
	if rep.Provider != "" {
		rows = append(rows, paint(cGhost, trunc(rep.Provider, width)))
	}
	head := fmtTokens(rep.Used)
	if rep.Window > 0 {
		head += "/" + fmtTokens(rep.Window) + " tokens  " + pct(rep.Used, rep.Window)
	} else {
		head += " tokens  ·  window unknown"
	}
	rows = append(rows, paint(cMuted, trunc(head, width)), "")

	// Said before the numbers rather than after them: every figure under this
	// line is a division of one measured total, or a guess when there was no
	// measurement to divide.
	note := "estimated usage by category"
	if rep.Calibrated {
		note = "measured prompt, split by estimate"
	}
	rows = append(rows, paint(cFaint+italic, trunc(note, width)))

	row := func(glyph, tone, label string, n int) string {
		line := paint(tone, glyph+" ") + paint(cMuted, label+": ") +
			paint(cInk, fmtTokens(n)+" tokens")
		if s := pct(n, total); s != "" {
			line += paint(cFaint, " ("+s+")")
		}
		return trunc(line, width)
	}
	for _, p := range rep.Parts {
		label := p.Label
		if p.Count > 0 {
			label += " (" + itoa(p.Count) + ")"
		}
		rows = append(rows, row("█", ctxColor(p.Key), label, p.Tokens))
	}
	if rep.Window > 0 {
		rows = append(rows, row("░", cGhost, "Free space", rep.Free))
	}
	return rows
}

// sectionRows are the things that are cheap because they are not loaded: how
// many there are, what the index for them costs, and that the rest is on disk
// until asked for. It is the other half of the lazy-loading bargain, and the
// only place the user can see the bargain being kept.
func (m *model) sectionRows(rep *ctxReport, width int) []string {
	var rows []string
	for _, s := range rep.Sections {
		if s.Count == 0 {
			continue
		}
		noun := s.Noun
		if s.Count != 1 && !strings.Contains(noun, " ") {
			noun += "s"
		}
		line := paint(cRule, "└ ") + paint(cMuted, padTo(itoa(s.Count)+" "+noun, 22))
		if s.Tokens > 0 {
			line += paint(cInk, fmtTokens(s.Tokens)+" tokens")
		} else {
			line += paint(cGhost, "no tokens")
		}
		if s.Hint != "" {
			line += paint(cGhost, sep+s.Hint)
		}
		rows = append(rows, margin+"  "+trunc(line, width))
	}
	return rows
}

// turnRow is the last round's behaviour, split into the two halves it actually
// spent its time on: thinking, then answering. One averaged rate hides exactly
// what people open this page to find — that the ninety seconds went to a thought
// nobody read, or that the answer itself crawled — so the halves get a row each
// and the round's own total sits under them.
func (m *model) turnRow(t ctxTurn, width int) []string {
	if t.Out == 0 && t.TPS == 0 {
		return nil
	}

	// tokens · rate · seconds, in that order on every row, so the three columns
	// line up down the page rather than each row reading its own way.
	phase := func(label, tone string, n int, est bool, tps, secs float64) string {
		if n == 0 && tps == 0 {
			return ""
		}
		count := fmtTokens(n) + " tokens"
		if est {
			count = "~" + count
		}
		cells := []string{paint(tone, padTo(label, 9)), paint(cInk, padTo(count, 14))}
		if tps > 0 {
			cells = append(cells, paint(cOK, padTo(fmt.Sprintf("%.0f tok/s", tps), 11)))
		} else {
			cells = append(cells, strings.Repeat(" ", 11))
		}
		if secs > 0 {
			cells = append(cells, paint(cFaint, fmtDur(time.Duration(secs*float64(time.Second)))))
		}
		return margin + "    " + trunc(strings.Join(cells, ""), width)
	}

	rows := []string{margin + "  " + paint(cInk+bold, "last turn")}
	for _, row := range []string{
		phase("thinking", cThink, t.Thinking, t.Est, t.ThinkTPS, t.ThinkSec),
		phase("answer", cMuted, t.Reply, t.ReplyEst, t.ReplyTPS, t.ReplySec),
		phase("total", cMuted, t.Out, false, t.TPS, t.Secs),
	} {
		if row != "" {
			rows = append(rows, row)
		}
	}
	if t.In > 0 {
		rows = append(rows, margin+"    "+
			trunc(paint(cGhost, "prompt "+fmtTokens(t.In)+" tokens, charged by the provider"), width))
	}
	return rows
}

// renderContext draws the whole page: grid, legend, the loaded-on-demand
// sections, and what the last round cost.
func (m *model) renderContext(b *block) []string {
	rep := b.ctx
	if rep == nil {
		return nil
	}

	width := m.bodyW()
	total := rep.Window
	if total <= 0 {
		total = rep.Used
	}
	cols, gridH, cells := gridShape(width - 2)
	gridW := cols*2 - 1
	counts := allocate(rep.Parts, total, cells)

	rows := []string{margin + paint(cRule, "▸ ") + paint(cInk+bold, "context")}

	grid := m.gridRows(rep, counts, cols, gridH)
	if width >= gridW+4+ctxLegend {
		legend := m.legendRows(rep, width-gridW-4)
		for i := 0; i < len(grid) || i < len(legend); i++ {
			left, right := strings.Repeat(" ", gridW), ""
			if i < len(grid) {
				left = grid[i]
			}
			if i < len(legend) {
				right = legend[i]
			}
			rows = append(rows, margin+"  "+left+"    "+right)
		}
	} else {
		// Too narrow to read side by side: the grid keeps its shape and the
		// legend goes underneath it rather than being squeezed to nothing.
		for _, g := range grid {
			rows = append(rows, margin+"  "+g)
		}
		rows = append(rows, "")
		for _, l := range m.legendRows(rep, width-2) {
			rows = append(rows, margin+"  "+l)
		}
	}

	if secs := m.sectionRows(rep, width-2); len(secs) > 0 {
		rows = append(rows, "", margin+"  "+paint(cInk+bold, "loaded on demand"))
		rows = append(rows, secs...)
	}
	// Four cells of indent under the heading, so the budget is two narrower than
	// the sections above it.
	if turn := m.turnRow(rep.Last, width-4); len(turn) > 0 {
		rows = append(rows, "")
		rows = append(rows, turn...)
	}

	foot := "counted here, not asked of the provider — /context costs nothing to run"
	if rep.Measured > 0 {
		foot = "shares scaled to the " + fmtTokens(rep.Measured) + " prompt the provider last charged for"
	}
	return append(rows, "", margin+"  "+paint(cGhost, trunc(foot, width-2)))
}
