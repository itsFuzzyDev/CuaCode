package protocol

import "encoding/json"

// Event is the typed view of one worker envelope. All payload fields are
// optional on the wire; absent strings are "" and absent counters are -1.
type Event struct {
	Envelope

	Status      string // explicit "status" key: running | tools | tooling | ...
	State       string // "state" key: startup | ready | done | error | ...
	Token       string // last streaming token
	Error       string // error text
	MsgCount    int    // total messages, -1 if not reported
	ContextLeft int    // tokens remaining, -1 if not reported
	ContextUsed int    // tokens spent, -1 if not reported
	ContextMax  int    // size of the model's window, -1 if not known
}

type eventPayload struct {
	Status      *string         `json:"status"`
	State       *string         `json:"state"`
	Token       json.RawMessage `json:"token"`
	Error       *string         `json:"error"`
	MsgCount    *int            `json:"msg_count"`
	ContextLeft *int            `json:"context_left"`
	ContextUsed *int            `json:"context_used"`
	ContextMax  *int            `json:"context_max"`
}

// decodeToken renders a token payload as text. The worker sends a JSON string
// for streamed text but an array for tool_calls, so anything that isn't a
// string is kept verbatim rather than dropping the whole event.
func decodeToken(raw json.RawMessage) string {
	var s string
	if err := json.Unmarshal(raw, &s); err == nil {
		return s
	}
	return string(raw)
}

// ParseEvent decodes one stdout line into a typed Event.
func ParseEvent(raw []byte) (Event, error) {
	ev := Event{MsgCount: -1, ContextLeft: -1, ContextUsed: -1, ContextMax: -1}
	if err := json.Unmarshal(raw, &ev.Envelope); err != nil {
		return ev, err
	}

	var p eventPayload
	// Payload keys are best-effort: an envelope whose data isn't an object
	// (or has differently-typed fields) is still a valid event.
	if err := json.Unmarshal(ev.Data, &p); err == nil {
		if p.Status != nil {
			ev.Status = *p.Status
		}
		if p.State != nil {
			ev.State = *p.State
		}
		if p.Token != nil {
			ev.Token = decodeToken(p.Token)
		}
		if p.Error != nil {
			ev.Error = *p.Error
		}
		if p.MsgCount != nil {
			ev.MsgCount = *p.MsgCount
		}
		if p.ContextLeft != nil {
			ev.ContextLeft = *p.ContextLeft
		}
		if p.ContextUsed != nil {
			ev.ContextUsed = *p.ContextUsed
		}
		if p.ContextMax != nil {
			ev.ContextMax = *p.ContextMax
		}
	}
	return ev, nil
}
