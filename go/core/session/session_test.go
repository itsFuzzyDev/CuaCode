package session

import (
	"os"
	"path/filepath"
	"testing"
	"time"

	"cuacode/core/protocol"
)

// fakeWorker emits a ready status, waits for the terminal reply, then
// answers every chat cmd with a token event and a done state.
const fakeWorker = `#!/bin/bash
echo '{"type":"status","id":"boot","data":{"state":"ready"}}'
read -r terminal_reply
echo "$terminal_reply" | grep -q '"type":"terminal"' || exit 1
while read -r line; do
  case "$line" in
  *'"chat"'*)
    echo '{"type":"stream","id":"t1","data":{"status":"running","token":"hi"}}'
    echo '{"type":"status","id":"t1","data":{"state":"done","msg_count":2,"context_left":9000}}'
    ;;
  *'"stop"'*) exit 0 ;;
  esac
done
`

func TestSessionRoundTrip(t *testing.T) {
	script := filepath.Join(t.TempDir(), "worker.sh")
	if err := os.WriteFile(script, []byte(fakeWorker), 0o755); err != nil {
		t.Fatal(err)
	}

	events := make(chan Event, 16)
	s := New(func(ev Event) { events <- ev }, Options{
		TerminalInfo: func() protocol.TerminalData { return protocol.TerminalData{TERM: "test"} },
	})
	if err := s.Start("/bin/bash", script); err != nil {
		t.Fatal(err)
	}
	defer s.Close()

	next := func() Event {
		select {
		case ev := <-events:
			return ev
		case <-time.After(2 * time.Second):
			t.Fatal("timed out waiting for event")
			return Event{}
		}
	}

	ev := next()
	if ev.Parsed.State != "ready" {
		t.Fatalf("want ready event, got %+v", ev.Parsed)
	}

	id, err := s.SendChat("hello")
	if err != nil {
		t.Fatal(err)
	}
	if id != "msg-1" {
		t.Fatalf("want msg-1, got %s", id)
	}
	if st := s.Snapshot(); st.State != Running || st.Msgs != 1 {
		t.Fatalf("after SendChat: %+v", st)
	}

	ev = next()
	if ev.Snapshot.State != Running || ev.Snapshot.LastToken != "hi" {
		t.Fatalf("token event: %+v", ev.Snapshot)
	}

	ev = next()
	st := ev.Snapshot
	if st.State != Done || st.Turns != 1 || st.Msgs != 2 || st.ContextLeft != 9000 {
		t.Fatalf("done event: %+v", st)
	}
	if !ev.StateChanged {
		t.Fatal("done event should report StateChanged")
	}

	if !s.MarkIdle() || s.Snapshot().State != Idle {
		t.Fatal("MarkIdle failed")
	}
}

// cancelWorker answers a chat with one token and then stalls, so the cancel
// has to arrive mid-run - the case the flag on the worker's reader thread
// exists for.
const cancelWorker = `#!/bin/bash
echo '{"type":"status","id":"boot","data":{"state":"ready"}}'
read -r terminal_reply
while read -r line; do
  case "$line" in
  *'"chat"'*) echo '{"type":"token","id":"t1","data":{"status":"running","token":"work"}}' ;;
  *'"cancel"'*)
    echo '{"type":"token","id":"t1","data":{"status":"cancelled","token":"cancelled","msg_count":1}}'
    ;;
  *'"stop"'*) exit 0 ;;
  esac
done
`

func TestSessionCancel(t *testing.T) {
	script := filepath.Join(t.TempDir(), "cancel.sh")
	if err := os.WriteFile(script, []byte(cancelWorker), 0o755); err != nil {
		t.Fatal(err)
	}

	events := make(chan Event, 16)
	s := New(func(ev Event) { events <- ev }, Options{
		TerminalInfo: func() protocol.TerminalData { return protocol.TerminalData{TERM: "test"} },
	})
	if err := s.Start("/bin/bash", script); err != nil {
		t.Fatal(err)
	}
	defer s.Close()

	next := func() Event {
		select {
		case ev := <-events:
			return ev
		case <-time.After(2 * time.Second):
			t.Fatal("timed out waiting for event")
			return Event{}
		}
	}

	next() // ready
	if _, err := s.SendChat("do something slow"); err != nil {
		t.Fatal(err)
	}
	if ev := next(); ev.Snapshot.State != Running {
		t.Fatalf("want running, got %+v", ev.Snapshot)
	}

	id, err := s.Cancel()
	if err != nil {
		t.Fatal(err)
	}
	if id != "cancel-2" {
		t.Fatalf("want cancel-2, got %s", id)
	}

	ev := next()
	if ev.Snapshot.State != Cancelled {
		t.Fatalf("want cancelled, got %+v", ev.Snapshot)
	}
	if !ev.StateChanged {
		t.Fatal("cancel should report StateChanged")
	}
	if ev.Snapshot.Turns != 0 {
		t.Fatalf("a cancelled run is not a completed turn: %+v", ev.Snapshot)
	}

	// A cancelled run is finished, so the idle timer applies to it too.
	if !s.MarkIdle() || s.Snapshot().State != Idle {
		t.Fatal("MarkIdle should accept Cancelled")
	}
}
