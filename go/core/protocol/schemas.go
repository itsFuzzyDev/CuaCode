package protocol

import "encoding/json"

type Envelope struct {
	Type string          `json:"type"`
	ID   string          `json:"id"`
	Data json.RawMessage `json:"data"`
}

type StatusData struct {
	State string `json:"state"`
	Text  string `json:"text,omitempty"`
}

type CmdData struct {
	Action string `json:"action"`
	Text   string `json:"text,omitempty"`
}

type TerminalData struct {
	TERM         string `json:"term"`
	Program      string `json:"term_program,omitempty"`
	TTY          string `json:"tty,omitempty"`
	FrontmostApp string `json:"frontmost_app,omitempty"`
	// Where the frontend was launched from, so a shell command starts in the
	// directory the user was standing in rather than somewhere arbitrary.
	// Omitted by frontends with no such directory; the worker then picks.
	CWD string `json:"cwd,omitempty"`
}
