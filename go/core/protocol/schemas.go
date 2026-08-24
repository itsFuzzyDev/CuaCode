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
	// What the user attached to this message. Name and payload travel
	// together: the worker hands the payload to the model and keeps the name
	// on the record, so a reopened conversation can still say what was sent
	// rather than "1 image".
	Images []Image `json:"images,omitempty"`
}

// Image is one attachment, as it crosses the wire: the file's name, and its
// bytes base64'd. Base64 rather than raw because the wire is line-delimited
// JSON, and the same encoding is what every provider wants at the other end —
// so nothing decodes it on the way through.
type Image struct {
	Name string `json:"name"`
	B64  string `json:"b64"`
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
