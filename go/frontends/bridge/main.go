// Command bridge is the GUI frontend for the computer-use agent: the same
// action tape deck draws in a terminal, drawn in a window.
//
// The split is deliberate. Go owns the worker, the session, and nothing else;
// every pixel is HTML and CSS under ui/. The host is the OS webview — WKWebView
// on macOS, WebView2 on Windows, WebKitGTK on Linux — so there is no bundled
// browser and no second runtime to ship.
//
// Two rules keep it cheap. Events are coalesced before they cross into JS
// (pump.go), and the page never re-renders a block it has already drawn
// (ui/app.js). Everything else is ordinary DOM.
//
// Like every frontend it only talks to core/runner and core/session — see
// go/frontends/deck for the terminal equivalent.
package main

import (
	"fmt"
	"os"
	"runtime"
	"strings"

	"cuacode/core/protocol"
	"cuacode/core/runner"
	"cuacode/core/session"

	webview "github.com/webview/webview_go"
)

const appName = "CuaCode"

const usage = `cuacode-bridge — GUI frontend

usage:
  bridge [--resume [id]]

  --resume        open the session picker
  --resume=<id>   reopen that session
  --serve         serve the page and print its url; no worker, no window
  -h, --help      this

env:
  CUACODE_DEBUG=1   open the webview's inspector
`

func main() {
	// The webview owns the thread its window was created on, and on macOS that
	// thread has to be the process's first one.
	runtime.LockOSThread()

	resume, resumeID, help := resumeFlag(os.Args[1:])
	if help {
		fmt.Print(usage)
		return
	}

	url, err := serveUI()
	if err != nil {
		die(err)
	}

	// --serve is the frontend's own feedback loop. The page is the whole of the
	// design, and a webview is a bad place to look at it from: nothing can open
	// the inspector from outside, take a screenshot of it, or read what it drew.
	// Served on its own it is an ordinary local page, so a browser — or anything
	// driving one — can open it, and ?demo replays a scripted conversation
	// through the same entry point the worker uses. No Python, no window, and
	// what you are looking at is the shipping UI rather than a mock of it.
	if has(os.Args[1:], "--serve") {
		fmt.Println(url)
		fmt.Println(url + "?demo")
		select {}
	}

	w := webview.New(os.Getenv("CUACODE_DEBUG") != "")
	defer w.Destroy()
	w.SetTitle(appName)
	w.SetSize(1000, 720, webview.HintNone)

	p := &pump{w: w}

	// TerminalInfo rather than nil: the worker starts shell commands in the
	// directory the frontend was launched from, and a GUI has one of those even
	// though it has no terminal.
	sess, err := runner.StartWith(p.emit, session.Options{
		TerminalInfo: func() protocol.TerminalData {
			return protocol.TerminalData{Program: appName, CWD: session.WorkingDir()}
		},
	})
	if err != nil {
		die(err)
	}
	defer sess.Close()

	bind(w, sess, p)

	// Asked for before the window opens, so the picker is already up — or the
	// conversation already replaying — by the time the first frame is drawn.
	switch {
	case resume && resumeID != "":
		p.loading = true
		sess.Command("session.load", map[string]any{"id": resumeID})
	case resume:
		sess.Command("session.list", nil)
	}

	w.Navigate(url)
	w.Run()
}

// bind exposes the session to the page. Every one of these runs on the UI
// thread: they are all a JSON write to the worker's stdin, which is a buffered
// pipe, so they are kept synchronous — a goroutine per call would buy nothing
// and would let two messages sent in quick succession swap order.
func bind(w webview.WebView, sess *session.Session, p *pump) {
	must(w.Bind("goSend", func(text string) { sess.SendChat(text) }))
	must(w.Bind("goCancel", func() { sess.Cancel() }))
	must(w.Bind("goBackground", func() { sess.Background() }))
	must(w.Bind("goCommand", func(action string, fields map[string]any) { sess.Command(action, fields) }))
	must(w.Bind("goReply", func(id, typ string, fields map[string]any) { sess.Reply(id, typ, fields) }))

	// The page says when it can receive. Worker events that land before the
	// first paint are held rather than evaluated into a document that does not
	// exist yet — the worker's startup line always beats the page.
	must(w.Bind("goReady", p.setReady))
}

// resumeFlag reads the resume flag off the command line. A bare --resume means
// "show me the list"; an id after it means that one, and a following flag is
// not an id.
func resumeFlag(args []string) (resume bool, id string, help bool) {
	for i := 0; i < len(args); i++ {
		switch arg := args[i]; {
		case arg == "-h", arg == "--help":
			return false, "", true

		case arg == "--resume", arg == "-r":
			resume = true
			if i+1 < len(args) && !strings.HasPrefix(args[i+1], "-") {
				i++
				id = args[i]
			}

		case strings.HasPrefix(arg, "--resume="):
			resume, id = true, strings.TrimPrefix(arg, "--resume=")
		}
	}
	return resume, id, false
}

func has(args []string, flag string) bool {
	for _, a := range args {
		if a == flag {
			return true
		}
	}
	return false
}

func must(err error) {
	if err != nil {
		die(err)
	}
}

func die(err error) {
	fmt.Fprintln(os.Stderr, err)
	os.Exit(1)
}
