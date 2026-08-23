package main

// The one seam between the worker and the page, and the place the frontend's
// efficiency is actually decided.
//
// A streaming round is hundreds of events a second, each carrying a few
// characters. Evaluating one script per event would spend the whole budget in
// the bridge rather than the renderer, so events are held for a beat, adjacent
// chunks of the same stream are concatenated, and one batch crosses per frame.
// A 900-token answer arrives as roughly forty small scripts instead of nine
// hundred, and the page sees text rather than a shower of fragments.

import (
	"encoding/json"
	"sync"
	"time"

	"cuacode/core/session"
)

// view is the part of the window the pump touches. Narrowed to an interface so
// the coalescing — the one piece of this frontend that can break without
// anything looking wrong — can be tested without opening a window.
type view interface {
	Eval(js string)
	Dispatch(fn func())
}

// flushEvery is how long an event waits for company. Long enough that a fast
// stream coalesces into meaningful chunks, short enough that a lone event —
// the answer to a keystroke — still feels immediate.
const flushEvery = 24 * time.Millisecond

// wireEvent is one worker event flattened for the page. It is deliberately the
// worker's own vocabulary rather than a second one invented here: the page
// switches on the same state names deck does, and a new worker state needs no
// change on this side of the bridge.
type wireEvent struct {
	State string          `json:"state"`
	Type  string          `json:"type,omitempty"`
	Token string          `json:"token,omitempty"`
	Err   string          `json:"error,omitempty"`
	Data  json.RawMessage `json:"data,omitempty"`
	Raw   string          `json:"raw,omitempty"` // set only for an unreadable line
}

// batch is what one flush hands the page: everything that happened since the
// last one, plus the state after all of it. The status goes once per batch
// rather than once per event — the bar can only show the latest reading, and
// forty intermediate copies of it are forty copies the page has to skip.
type batch struct {
	Events  []wireEvent      `json:"events"`
	Status  session.Snapshot `json:"status"`
	Loading bool             `json:"loading"`
}

type pump struct {
	w view

	mu      sync.Mutex
	events  []wireEvent
	snap    session.Snapshot
	pending bool // a flush is already scheduled
	ready   bool // the page has loaded and can be evaluated into
	loading bool // the next session event is a resume, not a new conversation
}

// emit is the session's notify callback. It runs on the worker's reader
// goroutine and must not touch the window, so all it does is append.
func (p *pump) emit(ev session.Event) {
	p.mu.Lock()
	if ev.ParseErr != nil {
		p.events = append(p.events, wireEvent{State: "bad_line", Raw: string(ev.Raw)})
	} else {
		p.append(wireEvent{
			State: ev.Parsed.State,
			Type:  ev.Parsed.Type,
			Token: ev.Parsed.Token,
			Err:   ev.Parsed.Error,
			Data:  ev.Parsed.Data,
		})
	}
	p.snap = ev.Snapshot
	schedule := !p.pending
	p.pending = true
	p.mu.Unlock()

	if schedule {
		time.AfterFunc(flushEvery, p.flush)
	}
}

// append adds one event, merging it into the previous when both are chunks of
// the same stream. Data is dropped on a merged event: for a text chunk it holds
// only the token, which is already in Token, and keeping the last fragment's
// copy would describe the merge rather than the text.
func (p *pump) append(ev wireEvent) {
	if !streaming(ev.State) {
		p.events = append(p.events, ev)
		return
	}
	ev.Data = nil
	if n := len(p.events); n > 0 && p.events[n-1].State == ev.State {
		p.events[n-1].Token += ev.Token
		return
	}
	p.events = append(p.events, ev)
}

// streaming reports whether a state carries a chunk of text that means the same
// thing concatenated as it does in pieces.
func streaming(state string) bool { return state == "thinking" || state == "content" }

// setReady is called by the page once it can be evaluated into. Whatever the
// worker said in the meantime is delivered immediately: a startup line held
// back is a window that opens blank.
func (p *pump) setReady() {
	p.mu.Lock()
	p.ready = true
	p.mu.Unlock()
	p.flush()
}

func (p *pump) flush() {
	p.mu.Lock()
	if !p.ready || len(p.events) == 0 {
		// Cleared even when nothing goes out, so the next event schedules a new
		// flush rather than waiting for one that has already run.
		p.pending = false
		p.mu.Unlock()
		return
	}
	b := batch{Events: p.events, Status: p.snap, Loading: p.loading}
	p.events, p.pending, p.loading = nil, false, false
	p.mu.Unlock()

	raw, err := json.Marshal(b)
	if err != nil {
		return
	}
	// Built here rather than inside the closure so the marshalling stays off
	// the UI thread; Dispatch only carries the finished string.
	js := "window.__cua.push(" + string(raw) + ")"
	p.w.Dispatch(func() { p.w.Eval(js) })
}
