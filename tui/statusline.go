package main

import (
	"fmt"
	"regexp"
	"strings"
)

// ANSI color/style helpers.  Drop these into your statusline string wherever
// you want color.  Always pair with Reset (or another color) so the style
// doesn't bleed into adjacent fields.
const (
	Reset      = "\x1b[0m"
	StyleReset = "\x1b[22;23;24;25;27;28;29;39m"
	Bg         = "\x1b[48;2;30;30;46m"
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

var ansiRe = regexp.MustCompile(`\x1b\[[0-9;]*m`)

// visualWidth counts display cells in a string that may contain ANSI escape
// sequences.  Use this when padding so gaps are based on what the eye sees,
// not raw byte length.
func visualWidth(s string) int {
	plain := ansiRe.ReplaceAllString(s, "")
	w := 0
	for _, r := range plain {
		if r == '\t' {
			w += 4
		} else {
			w++
		}
	}
	return w
}

// renderStatusline draws the line between the content pane and the input bar.
// This function is intentionally separated so you can customize it freely.
// It receives the full model, so you have access to:
//
//	Model fields:
//	  m.width, m.height       terminal dimensions
//	  m.scroll                content scroll position
//	  m.content               raw content lines
//	  m.inputRunes            current input buffer
//	  m.cursorPos             cursor position in input
//	  m.msgID                 message counter
//	  m.worker                the protocol worker (check nil)
//
//	Status fields (populated from worker events):
//	  m.status.State          "idle" | "running" | "tools" | "done" | "error"
//	  m.status.Msgs           total messages in conversation
//	  m.status.Turns          completed assistant response turns
//	  m.status.LastToken      last streaming token text
//	  m.status.Error          last error text
//	  m.status.ContextLeft    tokens remaining (worker must report "context_left")
//
// To add more fields, extend statusData in main.go and have the Python worker
// send the key in its JSON replies. The TUI will auto-extract float64/string
// values into the status struct.
//
// The returned string should not exceed m.width rune cells to avoid
// terminal auto-wrapping.
// thinkingFrames holds the animation cells shown next to the state when the
// worker is busy.  Each string is one frame.  Replace these with whatever
// pattern you want.  The animation loops forever at 120ms per frame while
// status is "running", "tools", or "tooling".
var thinkingFrames = []string{
	"⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏",
}

func renderStatusline(m *model) string {
	state := m.status.State
	if state == "" {
		state = "idle"
	}

	stateColor := Dim
	switch state {
	case "running":
		stateColor = Cyan
	case "tools", "tooling":
		stateColor = Yellow
	case "done":
		stateColor = Green
	case "error":
		stateColor = Red
	}

	anim := ""
	if state == "running" || state == "tools" || state == "tooling" {
		if len(thinkingFrames) > 0 {
			anim = thinkingFrames[m.thinkingFrame%len(thinkingFrames)] + "  "
		}
	}

	left := fmt.Sprintf("%s%s%s  %s%d msgs", stateColor, state, StyleReset, anim, m.status.Msgs)
	if m.status.Turns > 0 {
		left += fmt.Sprintf("  %d turns", m.status.Turns)
	}

	right := fmt.Sprintf("%d×%d  %d/%d", m.width, m.height, m.scroll, len(m.wrapped))
	if m.status.ContextLeft > 0 {
		right = fmt.Sprintf("ctx:%d  %s", m.status.ContextLeft, right)
	}

	// Pad with spaces based on *visual* width so ANSI codes don't throw off
	// alignment.
	gap := m.width - visualWidth(left) - visualWidth(right)
	if gap < 1 {
		gap = 1
	}
	if gap > m.width {
		gap = m.width
	}
	return left + strings.Repeat(" ", gap) + right
}
