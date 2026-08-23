package main

import (
	"encoding/json"
	"strings"
	"sync"
	"testing"
	"time"

	"cuacode/core/protocol"
	"cuacode/core/session"
)

// fakeView captures what would have been evaluated into the page, so a test can
// read the batch the page would have received.
type fakeView struct {
	mu   sync.Mutex
	js   []string
	done chan struct{}
	want int
}

func (f *fakeView) Eval(js string) {
	f.mu.Lock()
	f.js = append(f.js, js)
	n := len(f.js)
	f.mu.Unlock()
	if f.done != nil && n == f.want {
		close(f.done)
	}
}
func (f *fakeView) Dispatch(fn func()) { fn() }

func (f *fakeView) scripts() []string {
	f.mu.Lock()
	defer f.mu.Unlock()
	return append([]string(nil), f.js...)
}

// decode reads the batch back out of the one script the pump wrote.
func decode(t *testing.T, js string) batch {
	t.Helper()
	const prefix = "window.__cua.push("
	if !strings.HasPrefix(js, prefix) || !strings.HasSuffix(js, ")") {
		t.Fatalf("not a push call: %q", js)
	}
	var b batch
	if err := json.Unmarshal([]byte(js[len(prefix):len(js)-1]), &b); err != nil {
		t.Fatalf("batch is not valid JSON, so the page would have thrown: %v", err)
	}
	return b
}

func newPump(v *fakeView) *pump {
	p := &pump{w: v}
	p.ready = true
	return p
}

func ev(state, token string) session.Event {
	return session.Event{
		Parsed:   protocol.Event{State: state, Token: token},
		Snapshot: session.Snapshot{State: session.Running},
	}
}

// waitFor blocks until the view has been written to, so a test never races the
// pump's own flush timer.
func waitFor(t *testing.T, v *fakeView) {
	t.Helper()
	select {
	case <-v.done:
	case <-time.After(2 * time.Second):
		t.Fatal("pump never flushed")
	}
}

// A streaming round is hundreds of events a second. If they stop coalescing the
// app still works and nobody notices until it is evaluating a script per token,
// so the merge is worth a test even though nothing visible depends on it.
func TestChunksOfOneStreamMerge(t *testing.T) {
	v := &fakeView{done: make(chan struct{}), want: 1}
	p := newPump(v)

	for _, tok := range []string{"the ", "user ", "wants ", "notes"} {
		p.emit(ev("thinking", tok))
	}
	waitFor(t, v)

	b := decode(t, v.scripts()[0])
	if len(b.Events) != 1 {
		t.Fatalf("got %d events, want 1 merged: %+v", len(b.Events), b.Events)
	}
	if b.Events[0].Token != "the user wants notes" {
		t.Errorf("token = %q, want the whole thought", b.Events[0].Token)
	}
}

// Only chunks of the same stream may merge: thinking that ran into an answer
// would put the thought inside the reply.
func TestDifferentStatesDoNotMerge(t *testing.T) {
	v := &fakeView{done: make(chan struct{}), want: 1}
	p := newPump(v)

	p.emit(ev("thinking", "weighing it up"))
	p.emit(ev("content", "here is what I found"))
	p.emit(ev("thinking", "second thought"))
	waitFor(t, v)

	b := decode(t, v.scripts()[0])
	if len(b.Events) != 3 {
		t.Fatalf("got %d events, want 3 kept apart: %+v", len(b.Events), b.Events)
	}
	for i, want := range []string{"thinking", "content", "thinking"} {
		if b.Events[i].State != want {
			t.Errorf("event %d is %q, want %q", i, b.Events[i].State, want)
		}
	}
}

// A tool call is not text. Two of them arriving together must stay two, or a
// batch of calls turns into one row with both payloads concatenated.
func TestToolCallsNeverMerge(t *testing.T) {
	v := &fakeView{done: make(chan struct{}), want: 1}
	p := newPump(v)

	p.emit(ev("tool_calls", `[{"name":"click"}]`))
	p.emit(ev("tool_calls", `[{"name":"screenshot"}]`))
	waitFor(t, v)

	b := decode(t, v.scripts()[0])
	if len(b.Events) != 2 {
		t.Fatalf("got %d events, want 2: %+v", len(b.Events), b.Events)
	}
}

// The worker's first line beats the page's first paint every time. Held events
// have to survive the wait, or the window opens on an empty conversation that
// has already started.
func TestEventsBeforeTheFirstPaintAreHeld(t *testing.T) {
	v := &fakeView{done: make(chan struct{}), want: 1}
	p := &pump{w: v} // not ready

	p.emit(ev("thinking", "before the page existed"))
	time.Sleep(flushEvery * 3)

	if got := len(v.scripts()); got != 0 {
		t.Fatalf("wrote %d scripts into a page that had not loaded", got)
	}

	p.setReady()
	waitFor(t, v)

	b := decode(t, v.scripts()[0])
	if len(b.Events) != 1 || b.Events[0].Token != "before the page existed" {
		t.Fatalf("held event was lost: %+v", b.Events)
	}
}

// An unreadable line is the one thing the page cannot decode for itself, so it
// has to arrive already named as one rather than as a silent gap in the feed.
func TestUnreadableLineIsReported(t *testing.T) {
	v := &fakeView{done: make(chan struct{}), want: 1}
	p := newPump(v)

	p.emit(session.Event{ParseErr: errParse{}, Raw: []byte("{not json")})
	waitFor(t, v)

	b := decode(t, v.scripts()[0])
	if len(b.Events) != 1 || b.Events[0].State != "bad_line" {
		t.Fatalf("got %+v, want one bad_line event", b.Events)
	}
	if b.Events[0].Raw != "{not json" {
		t.Errorf("raw = %q, want the line itself so it can be shown", b.Events[0].Raw)
	}
}

// The bar can only show the latest reading, so one status per batch is all the
// page is given however many events it carries.
func TestOneStatusPerBatch(t *testing.T) {
	v := &fakeView{done: make(chan struct{}), want: 1}
	p := newPump(v)

	p.emit(ev("content", "a"))
	last := ev("content", "b")
	last.Snapshot = session.Snapshot{State: session.Done, ContextUsed: 4200}
	p.emit(last)
	waitFor(t, v)

	b := decode(t, v.scripts()[0])
	if b.Status.State != session.Done || b.Status.ContextUsed != 4200 {
		t.Fatalf("status = %+v, want the state after the whole batch", b.Status)
	}
}

type errParse struct{}

func (errParse) Error() string { return "unreadable" }
