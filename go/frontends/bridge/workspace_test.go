package main

import (
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"cuacode/core/protocol"
	"cuacode/core/session"
)

// The window tools park the app named in the terminal envelope's
// frontmost_app -- the agent's own window, into the left strip. A GUI
// frontend has no shell to inherit that name from, so bridge names its own
// process. If the envelope ever stops carrying it, nothing parks the agent's
// window, and app_open parks whatever happens to be frontmost instead.
func TestTerminalEnvelopeCarriesSelfApp(t *testing.T) {
	script := filepath.Join(t.TempDir(), "worker.sh")
	os.WriteFile(script, []byte(`#!/bin/bash
echo '{"type":"status","id":"boot","data":{"state":"ready"}}'
read -r line
echo "{\"type\":\"status\",\"id\":\"echo\",\"data\":{\"state\":\"echoed\",\"raw\":$line}}"
`), 0o755)

	events := make(chan session.Event, 16)
	s := session.New(func(ev session.Event) { events <- ev }, session.Options{
		TerminalInfo: func() protocol.TerminalData {
			self := appName
			if exe, err := os.Executable(); err == nil {
				self = filepath.Base(exe)
			}
			return protocol.TerminalData{Program: appName, CWD: "/tmp", FrontmostApp: self}
		},
	})
	if err := s.Start("/bin/bash", script); err != nil {
		t.Fatal(err)
	}
	defer s.Close()

	exe, _ := os.Executable()
	want := filepath.Base(exe)
	deadline := time.Now().Add(5 * time.Second)
	for time.Now().Before(deadline) {
		select {
		case ev := <-events:
			if ev.Parsed.State != "echoed" {
				continue
			}
			if !strings.Contains(string(ev.Parsed.Data), "frontmost_app") {
				t.Fatalf("terminal envelope carries no frontmost_app: %s", ev.Parsed.Data)
			}
			if !strings.Contains(string(ev.Parsed.Data), want) {
				t.Fatalf("frontmost_app is not the frontend's own process (%q): %s", want, ev.Parsed.Data)
			}
			return
		case <-time.After(100 * time.Millisecond):
		}
	}
	t.Fatal("worker never echoed the terminal envelope")
}