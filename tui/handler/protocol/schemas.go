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
