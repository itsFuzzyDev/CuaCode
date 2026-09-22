// appmenu_darwin.go gives the window the menu bar a bundled app would have had
// for free, and the name that goes with it.
//
// None of this is app behaviour: on macOS the standard shortcuts ARE menu items,
// so an app with no main menu has no Cmd+Q and no Cmd+H. A binary launched from
// a shell gets no main menu, because AppKit only builds one from a nib that only
// a bundle has. The webview library does set the activation policy to regular,
// so the app can own the menu bar once there is one to own.
//
// The name is the same story, and it is not a lever this file has. A binary with
// no bundle has no CFBundleName to read, so the menu bar shows the process name,
// which is the executable's path exactly as it was exec'd: bin/PANE reads PANE,
// bin/pane reads pane. `setProcessName:` sticks as far as processName is
// concerned, but AppKit has taken the menu bar's copy by then, from either order
// relative to sharedApplication. That is measured rather than assumed: one probe
// set the name before NSApplication and one after, and the menu bar read the
// executable's name in both.
//
// So the name is a filename problem. The frontend directory is capitalised for
// exactly this reason: build.sh names the binary after the directory, so
// `cuacode pane` execs bin/Pane and the menu bar says Pane.
//
// The items target nil, so the action travels the responder chain to NSApp the
// way AppKit's own menus do.

//go:build darwin

package main

/*
#cgo CFLAGS: -x objective-c
#cgo darwin LDFLAGS: -framework AppKit
#import <Cocoa/Cocoa.h>

// What the app calls itself. Not what the menu bar reads, which is the
// executable's file name (see above), but what processName reports and what the
// menu items are titled from.
static NSString *kAppName = @"Pane";

// Private, and declared here so the call is checked at the site that makes it
// instead of at runtime.
@interface NSProcessInfo (BridgeAppName)
- (void)setProcessName:(NSString *)name;
@end

static NSString *bridgeAppName(void) {
	NSProcessInfo *pi = [NSProcessInfo processInfo];
	if ([pi respondsToSelector:@selector(setProcessName:)]) {
		[pi setProcessName:kAppName];
	}
	// The name in hand, not the name asked for: on a system without the setter
	// the menu titles say what the menu bar says.
	return pi.processName;
}

static void bridgeAppMenu(void) {
	NSApplication *app = [NSApplication sharedApplication];
	NSString *name = bridgeAppName();

	NSMenu *appMenu = [[NSMenu alloc] init];
	[appMenu addItemWithTitle:[@"Hide " stringByAppendingString:name]
	                   action:@selector(hide:) keyEquivalent:@"h"];
	NSMenuItem *others = [appMenu addItemWithTitle:@"Hide Others"
	                                        action:@selector(hideOtherApplications:) keyEquivalent:@"h"];
	others.keyEquivalentModifierMask = NSEventModifierFlagCommand | NSEventModifierFlagOption;
	[appMenu addItemWithTitle:@"Show All"
	                   action:@selector(unhideAllApplications:) keyEquivalent:@""];
	[appMenu addItem:[NSMenuItem separatorItem]];
	[appMenu addItemWithTitle:[@"Quit " stringByAppendingString:name]
	                   action:@selector(terminate:) keyEquivalent:@"q"];

	NSMenuItem *appItem = [[NSMenuItem alloc] init];
	appItem.submenu = appMenu;

	NSMenu *main = [[NSMenu alloc] init];
	[main addItem:appItem];

	app.mainMenu = main;
}
*/
import "C"

// styleAppMenu names the app and installs its menu: quit, hide, hide others,
// show all. Called once, before the window runs; off macOS there is no menu bar
// to fill, so the stub stands.
func styleAppMenu() {
	C.bridgeAppMenu()
}
