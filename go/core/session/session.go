// Package session is the frontend-agnostic core of the app: it owns the
// worker process, tracks conversation state, and reports every change
// through a notify callback. A TUI, GUI, or CLI is just a consumer that
// renders Snapshots and forwards user input to SendChat.
package session

import (
	"encoding/json"
	"fmt"
	"sync"

	"cuacode/core/protocol"
)

// State is the coarse session state driven by worker events.
type State string

const (
	Idle      State = "idle"
	Running   State = "running"
	Tools     State = "tools"
	Done      State = "done"
	Error     State = "error"
	Cancelled State = "cancelled"
)

// Snapshot is an immutable copy of the session's observable state.
type Snapshot struct {
	State       State
	Msgs        int    // total messages in conversation
	Turns       int    // assistant response turns completed
	LastToken   string // last streaming token
	Error       string // last error text
	ContextLeft int    // tokens remaining (0 until the worker reports it)
	ContextUsed int    // tokens the conversation currently occupies
	ContextMax  int    // the model's window, 0 when its size is not known

	// The round that is generating, or the last one that did: what it wrote, how
	// much of that was thinking rather than answer, and how fast each half came
	// out. It moves live while the round streams — estimated from characters,
	// which TPSEst says out loud — and is replaced by the provider's own count
	// when the round is billed. Held afterwards, because it describes the round
	// it belongs to and stays true once that round is over.
	Phase       string // thinking | content, while one is streaming
	OutTokens   int
	ThinkTokens int
	ReplyTokens int
	ThinkEst    bool // the thinking figure was estimated, not billed
	TPS         float64
	ThinkTPS    float64
	ReplyTPS    float64
	TPSEst      bool // the rate came from characters, not from a token count
}

// Event is delivered to the notify callback for every worker line. It is a
// plain value: safe to send across goroutines, channels, or a UI event loop.
type Event struct {
	Parsed       protocol.Event // typed view; Envelope holds the raw data
	ParseErr     error          // non-nil if the line wasn't valid JSON
	Raw          []byte         // the original stdout line
	Snapshot     Snapshot       // state after applying this event
	StateChanged bool
}

// Options configures optional session behavior.
type Options struct {
	// TerminalInfo, when set, is sent to the worker automatically whenever
	// it reports state "ready" or "startup". Terminal frontends pass
	// ProbeTerminal; GUI frontends can leave it nil or supply their own.
	TerminalInfo func() protocol.TerminalData
}

type Session struct {
	notify func(Event)
	opts   Options

	mu     sync.Mutex
	worker *protocol.Worker
	snap   Snapshot
	msgSeq int
}

// New creates a session. notify may be nil; it is called from the worker's
// reader goroutine, so frontends should hand the event to their own loop
// (e.g. bubbletea's Program.Send) rather than mutate UI state directly.
func New(notify func(Event), opts Options) *Session {
	return &Session{
		notify: notify,
		opts:   opts,
		snap:   Snapshot{State: Idle},
	}
}

// Start spawns the worker process.
func (s *Session) Start(path string, args ...string) error {
	w, err := protocol.Spawn(s.handleLine, path, args...)
	if err != nil {
		return err
	}
	s.mu.Lock()
	s.worker = w
	s.mu.Unlock()
	return nil
}

// Snapshot returns a copy of the current state.
func (s *Session) Snapshot() Snapshot {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.snap
}

// SendChat sends a user message and returns its envelope ID.
func (s *Session) SendChat(text string) (string, error) {
	return s.SendChatWith(text, nil)
}

// SendChatWith is SendChat with attachments: pictures the user dropped onto the
// message, which the worker puts in front of the model as part of that turn.
//
// Sent on the same envelope as the text rather than on one of their own,
// because that is what they are — part of what the user said. A separate
// envelope would arrive as a second user message, which is a 400 on anthropic
// and a different turn on everything else.
func (s *Session) SendChatWith(text string, images []protocol.Image) (string, error) {
	s.mu.Lock()
	s.msgSeq++
	id := fmt.Sprintf("msg-%d", s.msgSeq)
	s.snap.Msgs++
	// Only when this message starts a run. Sent into one already going it is a
	// mid-turn message — the worker holds it and speaks it into the round in
	// flight — and the readouts belong to that round, which has not ended.
	// Clearing them there would blank a live rate every time the user typed.
	if s.snap.State != Running && s.snap.State != Tools {
		// The rate belonged to the round that just ended. Cleared rather than left
		// standing: a figure from the last answer, sitting next to a spinner for the
		// next one, reads as this round's and is not.
		s.snap.Phase, s.snap.TPS, s.snap.ThinkTPS, s.snap.ReplyTPS = "", 0, 0, 0
		s.snap.OutTokens, s.snap.ThinkTokens, s.snap.ReplyTokens = 0, 0, 0
		// Left alone otherwise, Tools included: a message typed while a tool is
		// running does not stop it being what the run is doing.
		s.snap.State = Running
	}
	w := s.worker
	s.mu.Unlock()

	if w == nil {
		return id, fmt.Errorf("session not started")
	}
	data, _ := json.Marshal(protocol.CmdData{Action: "chat", Text: text, Images: images})
	return id, w.SendEnv(protocol.Envelope{Type: "cmd", ID: id, Data: data})
}

// Cancel asks the worker to abandon the run in flight and returns the
// envelope ID. Unlike Close it leaves the worker alive and ready for the next
// message.
//
// It lands wherever the run currently is: while the request is opening, between
// streamed chunks, between tool calls, and inside one — a tool that watches for
// it stops, and one that does not is abandoned rather than waited for. The
// conversation is rewound past the partial turn either way, so nothing
// half-finished is carried into the next one. A cancel sent while the worker is
// idle is discarded rather than applied to whatever runs next.
func (s *Session) Cancel() (string, error) {
	s.mu.Lock()
	s.msgSeq++
	id := fmt.Sprintf("cancel-%d", s.msgSeq)
	w := s.worker
	s.mu.Unlock()

	if w == nil {
		return id, fmt.Errorf("session not started")
	}
	data, _ := json.Marshal(protocol.CmdData{Action: "cancel"})
	return id, w.SendEnv(protocol.Envelope{Type: "cmd", ID: id, Data: data})
}

// Background pushes the tool call currently running into the background and
// lets the turn carry on without it. The call keeps going under a job id; the
// agent is handed that id where it expected a result, and collects the real one
// later. Nothing is cancelled — this is the opposite of Cancel, for the case
// where the work is wanted and the waiting is not.
//
// It applies to the call in flight and nothing else. Sent while the worker is
// thinking rather than calling a tool, it is discarded rather than saved up for
// whatever runs next, so it is worth offering only while the UI shows a tool
// running.
func (s *Session) Background() (string, error) {
	s.mu.Lock()
	s.msgSeq++
	id := fmt.Sprintf("bg-%d", s.msgSeq)
	w := s.worker
	s.mu.Unlock()

	if w == nil {
		return id, fmt.Errorf("session not started")
	}
	data, _ := json.Marshal(protocol.CmdData{Action: "background"})
	return id, w.SendEnv(protocol.Envelope{Type: "cmd", ID: id, Data: data})
}

// Command sends an arbitrary worker command and returns its envelope ID. The
// worker's answer arrives through notify like any other line, matched by that
// ID. Chat and cancel have their own methods because they touch session state;
// everything else the worker accepts — session.list, session.new, provider.use
// — goes through here.
func (s *Session) Command(action string, fields map[string]any) (string, error) {
	s.mu.Lock()
	s.msgSeq++
	id := fmt.Sprintf("cmd-%d", s.msgSeq)
	w := s.worker
	s.mu.Unlock()

	if w == nil {
		return id, fmt.Errorf("session not started")
	}

	payload := map[string]any{"action": action}
	for k, v := range fields {
		if k != "action" {
			payload[k] = v
		}
	}
	data, err := json.Marshal(payload)
	if err != nil {
		return id, err
	}
	return id, w.SendEnv(protocol.Envelope{Type: "cmd", ID: id, Data: data})
}

// Reply answers a request the worker made of the frontend, echoing back the ID
// it asked under. The worker blocks until this arrives, so a request the
// frontend chooses to leave open stays open — that is the point of asking.
func (s *Session) Reply(id, typ string, fields map[string]any) error {
	s.mu.Lock()
	w := s.worker
	s.mu.Unlock()

	if w == nil {
		return fmt.Errorf("session not started")
	}
	data, err := json.Marshal(fields)
	if err != nil {
		return err
	}
	return w.SendEnv(protocol.Envelope{Type: typ, ID: id, Data: data})
}

// MarkIdle transitions Done -> Idle (frontends call this after an
// inactivity period). Returns true if the state changed.
func (s *Session) MarkIdle() bool {
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.snap.State != Done && s.snap.State != Cancelled {
		return false
	}
	s.snap.State = Idle
	return true
}

// Close asks the worker to stop, then kills the process.
func (s *Session) Close() error {
	s.mu.Lock()
	w := s.worker
	s.mu.Unlock()
	if w == nil {
		return nil
	}
	data, _ := json.Marshal(protocol.CmdData{Action: "stop"})
	_ = w.SendEnv(protocol.Envelope{Type: "cmd", ID: "quit", Data: data})
	return w.Kill()
}

// handleLine applies one worker stdout line to the state and notifies.
// Runs on the worker's reader goroutine.
func (s *Session) handleLine(raw []byte) {
	ev, perr := protocol.ParseEvent(raw)

	s.mu.Lock()
	old := s.snap.State
	if perr == nil {
		s.apply(ev)
	}
	snap := s.snap
	w := s.worker
	s.mu.Unlock()

	if perr == nil && (ev.State == "ready" || ev.State == "startup") &&
		s.opts.TerminalInfo != nil && w != nil {
		data, _ := json.Marshal(s.opts.TerminalInfo())
		_ = w.SendEnv(protocol.Envelope{Type: "terminal", ID: ev.ID, Data: data})
	}

	if s.notify != nil {
		s.notify(Event{
			Parsed:       ev,
			ParseErr:     perr,
			Raw:          raw,
			Snapshot:     snap,
			StateChanged: snap.State != old,
		})
	}
}

// apply folds a typed event into the snapshot. Caller holds s.mu.
func (s *Session) apply(ev protocol.Event) {
	if ev.Status != "" {
		if ev.Status == "tooling" {
			s.snap.State = Tools
		} else {
			s.snap.State = State(ev.Status)
		}
	} else {
		switch ev.State {
		case "error":
			s.snap.State = Error
		case "done":
			s.snap.State = Done
			s.snap.Turns++
		}
	}

	if ev.Token != "" {
		s.snap.LastToken = ev.Token
	}
	if ev.Error != "" {
		s.snap.Error = ev.Error
	}
	if ev.MsgCount >= 0 {
		s.snap.Msgs = ev.MsgCount
	}
	if ev.ContextLeft >= 0 {
		s.snap.ContextLeft = ev.ContextLeft
	}
	if ev.ContextUsed >= 0 {
		s.snap.ContextUsed = ev.ContextUsed
	}
	if ev.ContextMax > 0 {
		s.snap.ContextMax = ev.ContextMax
	}
	if ev.Phase != "" {
		s.snap.Phase = ev.Phase
	}
	if ev.OutTokens >= 0 {
		s.snap.OutTokens = ev.OutTokens
	}
	if ev.ThinkTokens >= 0 {
		s.snap.ThinkTokens, s.snap.ThinkEst = ev.ThinkTokens, ev.ThinkEst
	}
	if ev.ReplyTokens >= 0 {
		s.snap.ReplyTokens = ev.ReplyTokens
	}
	if ev.TPS >= 0 {
		s.snap.TPS, s.snap.TPSEst = ev.TPS, ev.TPSEst
	}
	if ev.ThinkTPS >= 0 {
		s.snap.ThinkTPS = ev.ThinkTPS
	}
	if ev.ReplyTPS >= 0 {
		s.snap.ReplyTPS = ev.ReplyTPS
	}
}

// Busy reports whether the given state should show activity (thinking
// animation, spinners) in a frontend.
func Busy(st State) bool {
	return st == Running || st == Tools
}
