package main

import (
	"strings"
	"testing"
)

// The terminal names its tab after the foreground process unless something
// tells it otherwise, so the title is the app and, once there is one, the
// conversation. Anything that could end the escape sequence early is gone by
// the time it is written.
func TestWindowTitle(t *testing.T) {
	if got := windowTitle(""); got != "cuacode" {
		t.Errorf("unnamed session: %q", got)
	}
	if got := windowTitle("  fix screenshot capture  "); got != "cuacode — fix screenshot capture" {
		t.Errorf("named session: %q", got)
	}
	if got := windowTitle("two\nlines\x07here"); strings.ContainsAny(got, "\n\x07") {
		t.Errorf("control characters survived into the title: %q", got)
	}
	if got := windowTitle(strings.Repeat("x", 200)); len([]rune(got)) > 80 {
		t.Errorf("title not clipped: %d runes", len([]rune(got)))
	}
}

// A name arrives from four places and only two of them are worth a line in the
// feed: the stub is the user's own words read back to them, and a reopened
// session already says which session it is.
func TestSessionTitleNoticedOnlyWhenChosen(t *testing.T) {
	m := initialModel()
	m.width, m.height = 100, 50

	m.fold(parse(t, `{"type":"status","id":"t1","data":{"state":"session_title","title":"fix the deck","source":"stub"}}`))
	if m.title != "fix the deck" {
		t.Errorf("stub title not kept: %q", m.title)
	}
	if n := m.tail(kNotice); n != nil && strings.Contains(plainOf(n.text), "named") {
		t.Error("a stub name should not be announced")
	}

	m.fold(parse(t, `{"type":"status","id":"t2","data":{"state":"session_title","title":"fix screenshot capture","source":"auto"}}`))
	if m.title != "fix screenshot capture" {
		t.Errorf("chosen title not kept: %q", m.title)
	}
	if n := m.tail(kNotice); n == nil || !strings.Contains(plainOf(n.text), "named · fix screenshot capture") {
		t.Error("a chosen name should be announced")
	}
}

// A dropped connection says so twice: once while it is being retried, and once
// at the end if it took the turn with it — including that what streamed before
// it went is still there.
func TestConnectionLostIsSaid(t *testing.T) {
	m := initialModel()
	m.width, m.height = 100, 50

	m.fold(parse(t, `{"type":"status","id":"r1","data":{"state":"retry","attempt":2,"of":4,"secs":4}}`))
	if n := m.tail(kNotice); n == nil || !strings.Contains(plainOf(n.text), "retry 2/4 in 4s") {
		t.Errorf("retry not reported: %+v", n)
	}

	m.fold(parse(t, `{"type":"token","id":"e1","data":{"state":"error","token":"Connection error.","status":"error","kept":true}}`))
	flat := ""
	for _, b := range m.blocks {
		flat += plainOf(b.text) + "\n"
	}
	if !strings.Contains(flat, "Connection error.") || !strings.Contains(flat, "partial reply kept") {
		t.Errorf("kept partial not reported:\n%s", flat)
	}
}
