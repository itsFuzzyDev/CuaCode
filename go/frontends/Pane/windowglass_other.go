//go:build !darwin

package main

import "unsafe"

// styleWindowGlass is a no-op off macOS, and the page is told so.
//
// There is nothing portable to call: window transparency is a platform feature
// on all three desktops, and every toolkit that offers it (Electron's vibrancy,
// Tauri's glass plugins, Wails) implements it per OS behind one flag. A port
// belongs here. Windows would be the first: DWM blurs behind a window, and the
// missing piece is not DWM but the webview, because webview_go hands out the
// window handle and nothing else. There is no way from here to stop the WebView2
// painting its own opaque background, which is what the blur would have to show
// through, so a Windows port means vendoring webview.h rather than adding a file
// next to this one.
//
// Until one lands, glassJS says nothing and the page paints the colour flat
// rather than tinted, so the app is dark instead of grey over the white webview.
func styleWindowGlass(unsafe.Pointer) {}

// startWindowDrag is a no-op off macOS, where the window keeps the system's own
// titlebar drag strip and the page's strip has nothing to hand back.
func startWindowDrag(unsafe.Pointer) {}

// glassJS is empty where the window was not cleared, which is how the page knows
// to paint its own colour instead of a tint over the webview's white.
func glassJS() string { return "" }
