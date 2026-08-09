// Command gio is the GUI frontend: a window that streams the worker's output
// and sends chat back. Like every frontend it only talks to core/runner and
// core/session — see go/frontends/classic for the terminal equivalent.
package main

import (
	"fmt"
	"os"
	"sync"

	"gioui.org/app"
	"gioui.org/op"
	"gioui.org/unit"

	"cuacode/core/protocol"
	"cuacode/core/runner"
	"cuacode/core/session"
)

const appName = "CuaCode"

func main() {
	go func() {
		if err := run(); err != nil {
			fmt.Fprintln(os.Stderr, err)
			os.Exit(1)
		}
		os.Exit(0)
	}()
	app.Main()
}

func run() error {
	var w app.Window
	w.Option(app.Title(appName), app.Size(unit.Dp(900), unit.Dp(640)))

	// Session events land on the worker's reader goroutine; queue them and
	// wake the window, which drains them while building the next frame.
	var q queue
	sess, err := runner.StartWith(
		func(ev session.Event) { q.push(ev); w.Invalidate() },
		session.Options{TerminalInfo: identity},
	)
	if err != nil {
		return err
	}
	defer sess.Close()

	u := newUI(newTheme(), sess)

	var ops op.Ops
	for {
		switch e := w.Event().(type) {
		case app.DestroyEvent:
			return e.Err

		case app.FrameEvent:
			gtx := app.NewContext(&ops, e)
			events := q.drain()
			for _, ev := range events {
				u.onEvent(ev)
			}
			u.Layout(gtx)
			if len(events) > 0 {
				// Invalidate can coalesce; one more frame guarantees the
				// last token of a burst is drawn.
				gtx.Execute(op.InvalidateCmd{})
			}
			e.Frame(gtx.Ops)
		}
	}
}

// identity is what the worker learns about its caller. The Python agent reads
// frontmost_app as its self-identity, so it reports our window rather than
// whatever terminal happened to launch us.
//
// No CWD: a window launched from the dock inherits / or the bundle path, and
// neither is a directory the user thinks they are in. Left out, so the worker
// falls back to home instead of dropping shell commands somewhere surprising.
func identity() protocol.TerminalData {
	return protocol.TerminalData{TERM: "gui", Program: appName, FrontmostApp: appName}
}

// queue carries worker events from the session goroutine to the UI goroutine.
// It is unbounded on purpose: dropping a token would corrupt the transcript.
type queue struct {
	mu     sync.Mutex
	events []session.Event
}

func (q *queue) push(ev session.Event) {
	q.mu.Lock()
	q.events = append(q.events, ev)
	q.mu.Unlock()
}

func (q *queue) drain() []session.Event {
	q.mu.Lock()
	defer q.mu.Unlock()
	events := q.events
	q.events = nil
	return events
}
