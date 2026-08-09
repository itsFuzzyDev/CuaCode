package main

// Everything the user sees is built here. The layout is deliberately plain —
// a feed, a status row, an input row — so there is something on screen to
// design against. The four seams worth pulling on:
//
//	feedLines   what one worker event becomes on screen (currently: raw envelope)
//	userLine    how a sent message is echoed into the feed
//	renderStatus the row between feed and input
//	render      the layout itself: panes, gutters, borders, whatever
//
// Available state on m: m.status (session.Snapshot — State, Msgs, Turns,
// LastToken, Error, ContextLeft), m.wrapped/m.scroll for the feed, m.width,
// m.height, m.spinFrame for animation.

import (
	"fmt"
	"regexp"
	"strings"

	"cuacode/core/session"
)

// ANSI helpers. Always close a color with StyleReset (or another color) so it
// doesn't bleed into the next field.
const (
	Reset      = "\x1b[0m"
	StyleReset = "\x1b[22;23;24;25;27;28;29;39m"
	ReverseOn  = "\x1b[7m"
	ReverseOff = "\x1b[27m"
	Bold       = "\x1b[1m"
	Dim        = "\x1b[2m"
	Red        = "\x1b[31m"
	Green      = "\x1b[32m"
	Yellow     = "\x1b[93m"
	Blue       = "\x1b[34m"
	Magenta    = "\x1b[35m"
	Cyan       = "\x1b[36m"
	Gray       = "\x1b[90m"
	White      = "\x1b[97m"
)

// prompt is the input marker; its width is also the feed's left inset.
const prompt = "> "

// spinFrames animate at 120ms/frame while the session is running or tooling.
var spinFrames = []string{"⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"}

var ansiRe = regexp.MustCompile(`\x1b\[[0-9;]*m`)

// visualWidth counts display cells, ignoring ANSI escapes. Pad with this, not
// len(), or colored fields will misalign.
func visualWidth(s string) int {
	w := 0
	for _, r := range ansiRe.ReplaceAllString(s, "") {
		if r == '\t' {
			w += 4
		} else {
			w++
		}
	}
	return w
}

// userLine echoes a sent message into the feed.
func userLine(text string) string {
	return Bold + prompt + StyleReset + text
}

// feedLines turns one worker event into feed lines. Right now it dumps the
// envelope verbatim, the way `classic` does — honest, unreadable, and the
// first thing a real design replaces.
//
// To render prose instead, key off the typed view: ev.Parsed.Token is the last
// streamed text chunk, ev.Parsed.Status / ev.Parsed.State the lifecycle keys,
// ev.Parsed.Error the failure text. Tokens arrive one chunk per event, so
// prose rendering means appending to the last line rather than adding one.
func feedLines(ev session.Event) []string {
	if ev.ParseErr != nil {
		return []string{Red + "[bad json] " + StyleReset + string(ev.Raw)}
	}
	env := ev.Parsed.Envelope
	return []string{
		fmt.Sprintf("%s[%s|%s]%s %s", Gray, env.Type, env.ID, StyleReset, env.Data),
	}
}

// stateColor maps a session state to its accent.
func stateColor(st session.State) string {
	switch st {
	case session.Running:
		return Cyan
	case session.Tools:
		return Yellow
	case session.Done:
		return Green
	case session.Error:
		return Red
	}
	return Dim
}

// renderStatus draws the row between the feed and the input.
func (m *model) renderStatus() string {
	st := m.status.State
	if st == "" {
		st = session.Idle
	}

	spin := "  "
	if session.Busy(st) {
		spin = spinFrames[m.spinFrame%len(spinFrames)] + " "
	}

	left := fmt.Sprintf("%s%s%s%s  %d msgs", spin, stateColor(st), st, StyleReset, m.status.Msgs)
	if m.status.Turns > 0 {
		left += fmt.Sprintf("  %d turns", m.status.Turns)
	}

	right := fmt.Sprintf("%d/%d", len(m.wrapped)-m.scroll, len(m.wrapped))
	if m.status.ContextLeft > 0 {
		right = fmt.Sprintf("ctx %d  %s", m.status.ContextLeft, right)
	}

	gap := max(m.width-visualWidth(left)-visualWidth(right), 1)
	return Dim + left + strings.Repeat(" ", gap) + right + StyleReset
}

// renderInput draws the input row. The buffer is single-line: when it outgrows
// the terminal it scrolls horizontally to keep the cursor in view.
func (m *model) renderInput() string {
	avail := max(m.width-len(prompt)-1, 1)

	start := 0
	if m.cursor > avail {
		start = m.cursor - avail
	}
	end := min(start+avail, len(m.input))
	visible := []rune(string(m.input[start:end]))

	col := m.cursor - start
	var b strings.Builder
	b.WriteString(Bold)
	b.WriteString(prompt)
	b.WriteString(StyleReset)
	under, rest := " ", ""
	if col < len(visible) {
		b.WriteString(string(visible[:col]))
		under, rest = string(visible[col]), string(visible[col+1:])
	} else {
		b.WriteString(string(visible))
	}
	b.WriteString(ReverseOn)
	b.WriteString(under)
	b.WriteString(ReverseOff)
	b.WriteString(rest)
	return b.String()
}

// writeRow writes one line padded to the full terminal width, so a repaint
// never leaves stale cells behind.
func (m *model) writeRow(b *strings.Builder, s string) {
	b.WriteString(s)
	if pad := m.width - visualWidth(s); pad > 0 {
		b.WriteString(strings.Repeat(" ", pad))
	}
}

// render composes the frame: feed, status row, input row.
func (m *model) render() string {
	if m.width == 0 || m.height == 0 {
		return "loading..."
	}
	if m.height < 3 {
		return "terminal too small\n"
	}

	var b strings.Builder

	h := m.contentHeight()
	start := max(len(m.wrapped)-h-m.scroll, 0)
	for i := range h {
		if idx := start + i; idx < len(m.wrapped) {
			m.writeRow(&b, m.wrapped[idx])
		} else {
			m.writeRow(&b, "")
		}
		b.WriteByte('\n')
	}

	m.writeRow(&b, m.renderStatus())
	b.WriteByte('\n')
	m.writeRow(&b, m.renderInput())

	b.WriteString(Reset)
	return b.String()
}
