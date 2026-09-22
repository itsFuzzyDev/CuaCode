// windowglass_darwin.go clears the window so the page can be a pane of glass.
//
// The design is the page's: ui/app.css paints #0A0E18 at 80% and nothing else.
// What that 80% shows is the blur this file puts underneath it, and the blur is
// what samples the desktop behind the window. A webview cannot do any of that
// from the page - backdrop-filter only ever samples the page's own backdrop -
// and the webview library exposes no transparency at all (no background color,
// no opaque flag, nothing), so the window has to be cleared from here.
//
// Three things, in this order:
//
//   - the window and the web view stop painting anything of their own. Left
//     opaque, the web view's own white sits under the page's 80% tint and the
//     app reads grey instead of #0A0E18 - which is exactly what no see-through
//     looks like.
//   - the web view moves into a container, so the blur is a plain sibling
//     UNDER it. A subview of a web view draws over the page, so the blur can
//     never go there; the container is the only spot that is unambiguously
//     behind the page and in front of the desktop.
//   - the window's own frame view is left alone. The traffic lights and the
//     titlebar drag region belong to it, which is why the container is built
//     instead of adding a view next to the content view.

//go:build darwin

package main

/*
#cgo CFLAGS: -x objective-c
#cgo darwin LDFLAGS: -framework AppKit -framework WebKit
#import <Cocoa/Cocoa.h>
#import <WebKit/WebKit.h>

static void bridgeGlass(void *p) {
	NSWindow *w = (NSWindow *)p;
	if (w == NULL) return;
	NSView *web = w.contentView;
	if (web == nil) return;

	w.opaque = NO;
	w.backgroundColor = [NSColor clearColor];

	// The page paints the tint and nothing else, so the web view must not paint
	// a background of its own. Three switches because which one works depends on
	// the WebKit in the OS: the property is the supported one since 12, the KVC
	// key is the one the webviews have used for a decade, and the layer is the
	// backstop underneath both. (The view's own `opaque` is read-only - it is
	// `isOpaque`, which only a subclass may override.)
	if ([web respondsToSelector:@selector(setUnderPageBackgroundColor:)]) {
		[web setValue:[NSColor clearColor] forKey:@"underPageBackgroundColor"];
	}
	@try { [web setValue:@NO forKey:@"drawsBackground"]; } @catch (NSException *ignored) {}
	if (web.layer) web.layer.backgroundColor = [NSColor clearColor].CGColor;

	NSView *box = [[NSView alloc] initWithFrame:web.frame];
	box.autoresizingMask = NSViewWidthSizable | NSViewHeightSizable;
	w.contentView = box;

	web.frame = box.bounds;
	web.autoresizingMask = NSViewWidthSizable | NSViewHeightSizable;

	NSVisualEffectView *fx = [[NSVisualEffectView alloc] initWithFrame:box.bounds];
	fx.autoresizingMask = NSViewWidthSizable | NSViewHeightSizable;
	fx.blendingMode = NSVisualEffectBlendingModeBehindWindow;
	// The material, and for behind-window blending it is a TINT, not a radius:
	// the window server blurs the backdrop by one fixed amount and every material
	// is that same blur at a different darkness. There is no radius to set
	// anywhere - no `blurRadius` key on the view, no ivar for one, and the private
	// backdrop layer under it exposes none either. 13 is
	// NSVisualEffectMaterialHUDWindow, picked by eye against a real desktop;
	// another material is one line away.
	fx.material = NSVisualEffectMaterialHUDWindow;
	fx.state = NSVisualEffectStateActive;

	[box addSubview:fx];
	[box addSubview:web];
}

// The drag, for a window that has no surface of its own to drag by.
//
// A non-opaque window is a frameless one as far as the window server's hit
// testing is concerned, and the system's titlebar drag strip goes with it - the
// same thing happens to every transparent window on every toolkit, which is why
// Electron has to spell out `-webkit-app-region: drag` for its own. So the page
// keeps the strip (ui/app.css #drag) and hands it back here on mousedown. The
// current event is that mousedown, and the window follows the mouse from there.
static void bridgeDrag(void *p) {
	NSWindow *w = (NSWindow *)p;
	if (w == NULL) return;
	NSEvent *e = [NSApp currentEvent];
	if (e != NULL) [w performWindowDragWithEvent:e];
}
*/
import "C"

import "unsafe"

func styleWindowGlass(w unsafe.Pointer) {
	if w != nil {
		C.bridgeGlass(w)
	}
}

func startWindowDrag(w unsafe.Pointer) {
	if w != nil {
		C.bridgeDrag(w)
	}
}

// glassJS is the flag the page needs to know it is in a real pane of glass.
// Injected at document start, where there is no element yet to hang a class on,
// so it leaves a flag behind and ui/app.js turns it into the class on <html>.
func glassJS() string {
	return "window.__cuaGlass = true;"
}
