// titlebar_darwin.go hands the window's top to the page.
//
// The webview is a titled NSWindow, and on macOS a webview cannot draw its own
// traffic lights or drag region - there is no frameless option, and porting to
// a framework that has one (Wails) is out of scope. But the mask does not have
// to change through the webview library: Window() hands back the NSWindow, and
// from there three settings make the titlebar disappear as a bar without
// giving up the buttons.
//
//   - titlebarAppearsTransparent: the bar keeps its geometry, loses its paint.
//   - titleVisibility hidden: no title text beside the lights - the page has
//     its own wordmark, and the Dock and Cmd-Tab still name the window.
//   - fullSizeContentView: the content view runs under that transparent bar,
//     so the page owns the full rectangle and the lights sit on the sidebar.
//
// The bar's last ~28px stays alive as chrome: it drags the window and
// double-click zooms it, which is what a user expects that strip to do, and it
// is drawn over the one row of the page that is not interactive anyway.
//
// The lights themselves are then stepped in from the window's corner - the
// system parks them tight against it, and the design wants them inset, on the
// sidebar's first row, with the wordmark aligned to their centreline (the
// same offset lives in app.css as body.native .brand). They stay the system's
// buttons: moved, not redrawn, so hover, click, and accessibility survive.

//go:build darwin

package main

/*
#cgo CFLAGS: -x objective-c
#cgo darwin LDFLAGS: -framework AppKit
#import <Cocoa/Cocoa.h>

// How far the lights step right and down from macOS's default parking spot,
// in points. app.css positions the wordmark around the same numbers
// (body.native .brand); change them together.
static const double lightShiftX = 10;
static const double lightShiftY = 8;

static void bridgeLights(void *p) {
	NSWindow *w = (NSWindow *)p;
	if (w == NULL) return;
	// A C array, not @[]: the standard buttons do not exist until the window
	// has been ordered front, and an NSArray literal raises on a nil entry
	// where this just skips one - at startup the shift lands via the
	// notification below, once the buttons are real.
	static NSButton *seen[3] = {nil, nil, nil};
	static NSPoint base[3];          // where macOS parked each one
	static NSPoint applied[3];       // where we last set it
	NSButton *btns[3] = {
		[w standardWindowButton:NSWindowCloseButton],
		[w standardWindowButton:NSWindowMiniaturizeButton],
		[w standardWindowButton:NSWindowZoomButton],
	};
	for (int i = 0; i < 3; i++) {
		NSButton *b = btns[i];
		if (b == nil) continue;
		NSView *sv = [b superview];
		if (sv == nil) continue;
		// The target is absolute: the system's own origin, recorded the
		// first time this button is seen, plus the shift. Relative nudging
		// would stack - every notification, restore, and resize would walk
		// the lights further across the sidebar.
		//
		// A frame that is neither our last apply nor this instance's
		// recorded base means AppKit re-parked the button on its own - a
		// re-layout no notification may cover. Whatever spot it chose is
		// the new default: record it, then shift from there. Without this
		// a re-park between notifications left the lights standing where
		// our last apply put them while the rest of the bar moved on.
		NSRect f = [b frame];
		if (seen[i] != b || !NSEqualPoints(f.origin, applied[i])) {
			seen[i] = b;
			base[i] = f.origin;
		}
		NSPoint p = [sv convertPoint:base[i] toView:nil];
		p.x += lightShiftX;
		p.y -= lightShiftY;
		f.origin = [sv convertPoint:p fromView:nil];
		[b setFrame:f];
		applied[i] = f.origin;
	}
}

// AppKit re-lays out the titlebar's buttons on resize and on (re)activation,
// which would silently undo the shift. Re-apply whenever the window says
// anything relevant happened. The selector-based observer needs an object to
// live on, so this is it.
@interface BridgeLightKeeper : NSObject
@end
@implementation BridgeLightKeeper
- (void)apply:(NSNotification *)n {
	bridgeLights([n object]);
	// And again next pass through the run loop: restore-from-Dock and resize
	// re-run the titlebar's own layout after the notification fires, which
	// would undo a single apply. Safe to run twice - the shift is absolute.
	dispatch_async(dispatch_get_main_queue(), ^{ bridgeLights([n object]); });
}
@end

static void bridgeTitlebar(void *p) {
	NSWindow *w = (NSWindow *)p;
	if (w == NULL) return;
	w.titlebarAppearsTransparent = YES;
	w.titleVisibility = NSWindowTitleHidden;
	w.styleMask |= NSWindowStyleMaskFullSizeContentView;

	// The page owns the whole rectangle, so the window's own background only
	// ever shows in the pixel it takes to round a corner or mis-cover a row:
	// set it to the page's canvas, and name the appearance dark, or the frame
	// draws its chrome in system-light colours over a dark app.
	w.backgroundColor = [NSColor colorWithCalibratedWhite:0.094 alpha:1.0];
	w.appearance = [NSAppearance appearanceNamed:NSAppearanceNameDarkAqua];

	bridgeLights(w);

	static BridgeLightKeeper *keeper = nil;
	if (keeper == nil) {
		keeper = [BridgeLightKeeper new];
		NSNotificationCenter *nc = [NSNotificationCenter defaultCenter];
		[nc addObserver:keeper selector:@selector(apply:)
		           name:NSWindowDidResizeNotification object:nil];
		[nc addObserver:keeper selector:@selector(apply:)
		           name:NSWindowDidDeminiaturizeNotification object:nil];
		[nc addObserver:keeper selector:@selector(apply:)
		           name:NSWindowDidBecomeKeyNotification object:nil];
	}
}
*/
import "C"

import "unsafe"

func styleTitlebar(w unsafe.Pointer) {
	if w != nil {
		C.bridgeTitlebar(w)
	}
}

// reapplyLights re-parks the traffic lights after AppKit re-lays the titlebar
// for a reason no notification covers - a title change among them. The apply
// is absolute, so this is safe to call whenever; it is also cheap (three
// setFrames) and runs only on the events that can move the lights.
func reapplyLights(w unsafe.Pointer) {
	if w != nil {
		C.bridgeLights(w)
	}
}
