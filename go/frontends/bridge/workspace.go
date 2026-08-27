package main

import (
	"fmt"
	"sync"

	"cuacode/core/protocol"
	"cuacode/core/runner"
	"cuacode/core/session"

	webview "github.com/webview/webview_go"
)

// workspace owns the open sessions. Each session is a separate worker process
// with its own pump; the page shows one at a time and the bindings route to the
// active one. Sessions are independent processes, so one running a long tool
// call never blocks another - the pumps coalesce each stream separately and the
// page only lays out the feed it is showing.
type workspace struct {
	w webview.WebView

	mu       sync.Mutex
	sessions map[string]*wsSession
	active   string
	seq      int
	ready    bool // the page has loaded and can be evaluated into
}

type wsSession struct {
	id   string
	sess *session.Session
	pump *pump
}

func newWorkspace(w webview.WebView) *workspace {
	return &workspace{w: w, sessions: map[string]*wsSession{}}
}

// newSession spawns a fresh worker and makes it active. The first session is
// "default" - the page boots with that id - and later ones are s2, s3, ...
func (ws *workspace) newSession() (string, error) {
	ws.mu.Lock()
	ws.seq++
	id := "default"
	if ws.seq > 1 {
		id = fmt.Sprintf("s%d", ws.seq)
	}
	ready := ws.ready
	ws.mu.Unlock()

	p := &pump{w: ws.w, session: id}
	// A session created after the page loaded can be evaluated into at once; a
	// held-back startup line would be a window that opens blank.
	if ready {
		p.ready = true
	}
	sess, err := runner.StartWith(p.emit, session.Options{
		TerminalInfo: func() protocol.TerminalData {
			return protocol.TerminalData{Program: appName, CWD: session.WorkingDir()}
		},
	})
	if err != nil {
		return "", err
	}

	ws.mu.Lock()
	ws.sessions[id] = &wsSession{id: id, sess: sess, pump: p}
	ws.active = id
	ws.mu.Unlock()
	return id, nil
}

// activeSess returns the session the bindings should talk to.
func (ws *workspace) activeSess() *wsSession {
	ws.mu.Lock()
	defer ws.mu.Unlock()
	return ws.sessions[ws.active]
}

// switchTo makes a session active. Unknown ids are ignored.
func (ws *workspace) switchTo(id string) {
	ws.mu.Lock()
	if _, ok := ws.sessions[id]; ok {
		ws.active = id
	}
	ws.mu.Unlock()
}

// close shuts a session's worker down and returns the new active id ("" if the
// last one went). Closing the active session moves to whatever is left.
func (ws *workspace) close(id string) string {
	ws.mu.Lock()
	s, ok := ws.sessions[id]
	if ok {
		delete(ws.sessions, id)
	}
	if ws.active == id {
		ws.active = ""
		for nid := range ws.sessions {
			ws.active = nid
			break
		}
	}
	next := ws.active
	ws.mu.Unlock()

	if ok {
		s.sess.Close()
	}
	return next
}

// setReady marks every pump ready once the page can be evaluated into, so the
// startup lines held back by each worker are delivered.
func (ws *workspace) setReady() {
	ws.mu.Lock()
	ws.ready = true
	all := make([]*wsSession, 0, len(ws.sessions))
	for _, s := range ws.sessions {
		all = append(all, s)
	}
	ws.mu.Unlock()
	for _, s := range all {
		s.pump.setReady()
	}
}

// closeAll shuts every worker down, for the frontend's own exit.
func (ws *workspace) closeAll() {
	ws.mu.Lock()
	all := make([]*wsSession, 0, len(ws.sessions))
	for _, s := range ws.sessions {
		all = append(all, s)
	}
	ws.mu.Unlock()
	for _, s := range all {
		s.sess.Close()
	}
}
