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

	// What the round generated, and how fast — live from characters while it is
	// still arriving, then again from the provider's own count once the round is
	// billed. -1 where the worker said nothing: a provider that reports no usage
	// reports none of this either, and a zero would read as a measurement rather
	// than as a silence.
	Phase       string  // which half is generating right now: thinking | content
	OutTokens   int     // tokens of reply, thinking included
	ThinkTokens int     // ...of which thinking, when it can be told apart
	ReplyTokens int     // ...and of which answer
	ThinkEst    bool    // the thinking figure was estimated, not billed
	TPS         float64 // tokens per second across the round, -1 if not reported
	ThinkTPS    float64 // ...while it was thinking
	ReplyTPS    float64 // ...while it was answering
	TPSEst      bool    // the rate came from characters, not from a token count

	// What a replayed user turn had attached to it, by name. Names only: the
	// worker does not send the payloads back when a conversation is reopened,
	// because a frontend redrawing a session wants to say a picture was there,
	// not to be handed megabytes of it again.
	Images []string
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
	Phase       *string         `json:"phase"`
	OutTokens   *int            `json:"out_tokens"`
	ThinkTokens *int            `json:"thinking_tokens"`
	ReplyTokens *int            `json:"reply_tokens"`
	ThinkEst    *bool           `json:"thinking_est"`
	TPS         *float64        `json:"tps"`
	ThinkTPS    *float64        `json:"think_tps"`
	ReplyTPS    *float64        `json:"reply_tps"`
	TPSEst      *bool           `json:"tps_est"`
	Images      []struct {
		Name string `json:"name"`
	} `json:"images"`
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
	ev := Event{MsgCount: -1, ContextLeft: -1, ContextUsed: -1, ContextMax: -1,
		OutTokens: -1, ThinkTokens: -1, ReplyTokens: -1, TPS: -1, ThinkTPS: -1, ReplyTPS: -1}
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
		if p.Phase != nil {
			ev.Phase = *p.Phase
		}
		if p.OutTokens != nil {
			ev.OutTokens = *p.OutTokens
		}
		if p.ThinkTokens != nil {
			ev.ThinkTokens = *p.ThinkTokens
		}
		if p.ReplyTokens != nil {
			ev.ReplyTokens = *p.ReplyTokens
		}
		if p.ThinkEst != nil {
			ev.ThinkEst = *p.ThinkEst
		}
		if p.TPS != nil {
			ev.TPS = *p.TPS
		}
		if p.ThinkTPS != nil {
			ev.ThinkTPS = *p.ThinkTPS
		}
		if p.ReplyTPS != nil {
			ev.ReplyTPS = *p.ReplyTPS
		}
		if p.TPSEst != nil {
			ev.TPSEst = *p.TPSEst
		}
		for _, img := range p.Images {
			ev.Images = append(ev.Images, img.Name)
		}
	}
	return ev, nil
}
