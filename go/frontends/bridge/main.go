// Command bridge is the GUI frontend for the computer-use agent: the same
// action tape deck draws in a terminal, drawn in a window.
//
// The split is deliberate. Go owns the worker, the session, and nothing else;
// every pixel is HTML and CSS under ui/. The host is the OS webview - WKWebView
// on macOS, WebView2 on Windows, WebKitGTK on Linux - so there is no bundled
// browser and no second runtime to ship.
//
// Two rules keep it cheap. Events are coalesced before they cross into JS
// (pump.go), and the page never re-renders a block it has already drawn
// (ui/app.js). Everything else is ordinary DOM.
//
// Like every frontend it only talks to core/runner and core/session - see
// go/frontends/deck for the terminal equivalent.
package main

import (
	"fmt"
	"os"
	"runtime"
	"strings"

	"cuacode/core/attach"
	"cuacode/core/protocol"

	webview "github.com/webview/webview_go"
)

const appName = "CuaCode"

const usage = `cuacode-bridge - GUI frontend

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
	// Served on its own it is an ordinary local page, so a browser - or anything
	// driving one - can open it, and ?demo replays a scripted conversation
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

	ws := newWorkspace(w)
	if _, err := ws.newSession(); err != nil {
		die(err)
	}
	defer ws.closeAll()

	bind(w, ws)

	// Asked for before the window opens, so the picker is already up - or the
	// conversation already replaying - by the time the first frame is drawn.
	switch {
	case resume && resumeID != "":
		ws.activeSess().pump.loading = true
		ws.activeSess().sess.Command("session.load", map[string]any{"id": resumeID})
	case resume:
		ws.activeSess().sess.Command("session.list", nil)
	}

	w.Navigate(url)
	w.Run()
}

// bind exposes the session to the page. Every one of these runs on the UI
// thread: they are all a JSON write to the worker's stdin, which is a buffered
// pipe, so they are kept synchronous - a goroutine per call would buy nothing
// and would let two messages sent in quick succession swap order. The session
// bindings route to the active session; the workspace bindings manage the set.
func bind(w webview.WebView, ws *workspace) {
	must(w.Bind("goSend", func(text string) { ws.activeSess().sess.SendChat(text) }))
	// The same message with pictures dropped or pasted onto it. A second
	// binding rather than a second argument on the first: the page calls
	// whichever it needs, and one served on its own with no bindings at all
	// still runs either way.
	must(w.Bind("goSendWith", func(text string, images []protocol.Image) {
		ws.activeSess().sess.SendChatWith(text, images)
	}))
	// The clipboard, read by the OS rather than by the page.
	//
	// A paste event is supposed to carry the picture in clipboardData, and in
	// a browser it does. In a webview it frequently does not - WKWebView hands
	// over an empty file list for an image copied by anything but itself - and
	// the page has no way to tell that apart from "there was no picture". So it
	// asks here, and this is the same reader the terminal frontend uses, which
	// is the point of it living in core.
	//
	// Synchronous like the rest, and unlike the rest it runs a program: a
	// couple of hundred milliseconds on the UI thread, once, on a keypress the
	// user is waiting for the result of anyway.
	must(w.Bind("goClipboard", func() (attach.Image, error) { return attach.Clipboard() }))

	must(w.Bind("goCancel", func() { ws.activeSess().sess.Cancel() }))
	must(w.Bind("goBackground", func() { ws.activeSess().sess.Background() }))
	must(w.Bind("goCommand", func(action string, fields map[string]any) { ws.activeSess().sess.Command(action, fields) }))
	must(w.Bind("goReply", func(id, typ string, fields map[string]any) { ws.activeSess().sess.Reply(id, typ, fields) }))

	// The window's own name, set from the page. The webview does not follow
	// document.title on any of the three hosts, and the title is the only part
	// of the app visible when it is not the front window - in a dock, in a task
	// switcher, in a list of windows - which is exactly when "CuaCode" alone
	// stops being enough to tell two of them apart.
	must(w.Bind("goTitle", func(title string) {
		if title = strings.TrimSpace(title); title != "" {
			w.SetTitle(title)
		}
	}))

	// The page says when it can receive. Worker events that land before the
	// first paint are held rather than evaluated into a document that does not
	// exist yet - the worker's startup line always beats the page.
	must(w.Bind("goReady", ws.setReady))

	// The workspace: start a fresh worker, switch which one is active, or close
	// one. goNewSession and goClose return the id the page should show next.
	must(w.Bind("goNewSession", func() string {
		id, err := ws.newSession()
		if err != nil {
			return ""
		}
		return id
	}))
	must(w.Bind("goSwitch", func(id string) { ws.switchTo(id) }))
	must(w.Bind("goClose", func(id string) string { return ws.close(id) }))
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
