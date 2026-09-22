// Command pane is the shell the design draws itself in.
//
// pane is its own app, not the current bridge with a new skin, so this file
// is only the launcher: it puts up the webview, serves ui/ to it, and hands the
// page a small set of bindings. Everything the page needs from Go arrives
// through bind(), which is the one place to hang a new call on.
//
// The host is the OS webview - WKWebView on macOS, WebView2 on Windows,
// WebKitGTK on Linux - so there is no bundled browser and no second runtime to
// ship. ui/ is hand-written HTML, CSS and JS embedded with go:embed
// (assets.go); no framework and no build step, on purpose.
package main

import (
	"fmt"
	"os"
	"runtime"
	"strings"

	webview "github.com/webview/webview_go"
)

const appName = "Pane"

const usage = `pane - GUI frontend

usage:
  pane [--serve]

  --serve         serve the page and print its url; no window
  -h, --help      this

env:
  CUACODE_DEBUG=1   open the webview's inspector
`

func main() {
	// The webview owns the thread its window was created on, and on macOS that
	// thread has to be the process's first one.
	runtime.LockOSThread()

	if has(os.Args[1:], "-h") || has(os.Args[1:], "--help") {
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
	// driving one - can open it.
	if has(os.Args[1:], "--serve") {
		fmt.Println(url)
		select {}
	}

	w := webview.New(os.Getenv("CUACODE_DEBUG") != "")
	defer w.Destroy()
	w.SetTitle(appName)
	w.SetSize(1200, 720, webview.HintNone)
	styleTitlebar(w.Window())
	// The window as glass: cleared, with the desktop blurred behind it, so the
	// page's tint has something to show (windowglass_darwin.go; a no-op off
	// macOS).
	styleWindowGlass(w.Window())
	// Whether it really is see-through is the page's business, because the same
	// tint is right on glass and wrong on an opaque window, where it lands on the
	// webview's own white and reads grey rather than dark (ui/app.css). Injected
	// rather than bound, so it is in place before the first paint, and empty on a
	// platform that has no glass.
	if js := glassJS(); js != "" {
		w.Init(js)
	}
	// The app menu: on macOS the standard shortcuts are menu items, and a binary
	// launched from a shell has no menu to carry them (appmenu_darwin.go).
	styleAppMenu()

	bind(w)

	w.Navigate(url)
	w.Run()
}

// bind exposes Go to the page. Every one of these runs on the UI thread, so
// keep them short and synchronous; a goroutine per call buys nothing.
//
// This is the seam for the whole app. Whatever pane turns out to be, its
// calls into Go are declared here, and the page reaches them through go()
// (ui/app.js), which is a no-op when the page is served on its own.
func bind(w webview.WebView) {
	// The window's own name, set from the page. The webview does not follow
	// document.title on any of the three hosts, and the title is the only part
	// of the app visible when it is not the front window.
	must(w.Bind("goTitle", func(title string) {
		if title = strings.TrimSpace(title); title != "" {
			w.SetTitle(title)
			// A title change re-lays the titlebar and no notification covers
			// it, so the lights drift. Park them again.
			reapplyLights(w.Window())
		}
	}))

	// The titlebar drag, handed back by the page: the window is non-opaque for
	// the glass, which costs it the system's own drag strip
	// (windowglass_darwin.go, ui/app.js).
	must(w.Bind("goDrag", func() { startWindowDrag(w.Window()) }))
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
