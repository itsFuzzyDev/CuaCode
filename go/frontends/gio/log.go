package main

import (
	"encoding/json"
	"fmt"
	"image/color"

	"cuacode/core/protocol"
	"cuacode/core/session"
)

// maxLines caps the scrollback so a long session can't grow without bound.
const maxLines = 5000

// Line is one envelope on the wire, rendered as a single row: a fixed-width
// mono prefix naming the direction, type and id, then the raw JSON payload.
type Line struct {
	Prefix  string
	Payload string
	Color   color.NRGBA // the prefix hue; the payload is always body text
}

// Log is the scrollback: every envelope in and out, in order, unparsed. It
// holds no UI state so it stays testable on its own.
type Log struct {
	lines []Line
}

func (l *Log) Lines() []Line { return l.lines }

func (l *Log) add(ln Line) {
	l.lines = append(l.lines, ln)
	if len(l.lines) > maxLines {
		l.lines = l.lines[len(l.lines)-maxLines:]
	}
}

// Sent records an envelope the frontend wrote to the worker.
func (l *Log) Sent(id, payload string) {
	l.add(Line{Prefix: prefix("→", "cmd", id), Payload: payload, Color: colIris})
}

// Note records something the frontend itself has to say, such as a send that
// failed before it reached the worker.
func (l *Log) Note(text string) {
	l.add(Line{Prefix: prefix("!", "gui", ""), Payload: text, Color: colRed})
}

// Received records one worker line exactly as it arrived.
func (l *Log) Received(ev session.Event) {
	if ev.ParseErr != nil {
		l.add(Line{Prefix: prefix("←", "?", ""), Payload: string(ev.Raw), Color: colRed})
		return
	}

	env := ev.Parsed.Envelope
	l.add(Line{
		Prefix:  prefix("←", env.Type, env.ID),
		Payload: string(env.Data),
		Color:   lineColor(ev.Parsed),
	})
}

// cmdPayload mirrors the envelope body the session writes, so the log shows
// the bytes that went out rather than a paraphrase of them.
func cmdPayload(cmd protocol.CmdData) string {
	b, err := json.Marshal(cmd)
	if err != nil {
		return "{}"
	}
	return string(b)
}

// prefix lays the row gutter out in fixed columns. Mono makes the padding
// line up, so type and id read as columns rather than prose.
func prefix(dir, typ, id string) string {
	return fmt.Sprintf("%s %-6s %-8s", dir, clamp(typ, 6), clamp(id, 8))
}

func clamp(s string, n int) string {
	if len(s) <= n {
		return s
	}
	return s[:n-1] + "…"
}

// lineColor tints the prefix by what the worker reported, so the eye can find
// tool calls and failures in a wall of tokens.
func lineColor(ev protocol.Event) color.NRGBA {
	switch {
	case ev.State == "error" || ev.Status == "error":
		return colRed
	case ev.State == "cancelled" || ev.Status == "cancelled" || ev.State == "cancel_ack":
		return colIris
	case ev.State == "done" || ev.Status == "done":
		return colMint
	case ev.Status == "tooling":
		return colAmber
	case ev.Status == "running":
		return colCyan
	default:
		return colMute
	}
}
