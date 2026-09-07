// picker_darwin.go opens the native folder picker. webview_go has no dialog
// API, and starting a session somewhere new needs one: NSOpenPanel, modal on
// the UI thread like any AppKit picker. darwin-first, the same pattern as
// titlebar_darwin.go; picker_other.go is the stub elsewhere.

//go:build darwin

package main

/*
#cgo CFLAGS: -x objective-c
#cgo darwin LDFLAGS: -framework AppKit -framework UniformTypeIdentifiers
#import <Cocoa/Cocoa.h>
#import <UniformTypeIdentifiers/UniformTypeIdentifiers.h>
#import <stdlib.h>

static char *bridgePickFolder(void) {
	NSOpenPanel *panel = [NSOpenPanel openPanel];
	[panel setCanChooseDirectories:YES];
	[panel setCanChooseFiles:NO];
	[panel setAllowsMultipleSelection:NO];
	[panel setPrompt:@"Choose project"];
	[panel setMessage:@"Where should this session work?"];
	if ([panel runModal] != NSModalResponseOK) return NULL;
	NSString *path = [panel.URLs.firstObject path];
	if (path == nil) return NULL;
	return strdup(path.UTF8String);
}
static char *bridgePickImage(void) {
	NSOpenPanel *panel = [NSOpenPanel openPanel];
	[panel setCanChooseDirectories:NO];
	[panel setCanChooseFiles:YES];
	[panel setAllowsMultipleSelection:NO];
	[panel setAllowedContentTypes:@[[UTType typeWithIdentifier:@"public.image"]]];
	[panel setPrompt:@"Choose icon"];
	[panel setMessage:@"Pick an image for this project"];
	if ([panel runModal] != NSModalResponseOK) return NULL;
	NSString *path = [panel.URLs.firstObject path];
	if (path == nil) return NULL;
	NSError *err = nil;
	NSData *d = [NSData dataWithContentsOfFile:path options:0 error:&err];
	if (d == nil || d.length > 8 * 1024 * 1024) return NULL;   // 8MB ceiling, MAX_IMAGE's own
	NSString *ext = path.pathExtension.lowercaseString;
	NSString *mime = @"image/png";
	if ([ext isEqualToString:@"jpg"] || [ext isEqualToString:@"jpeg"]) mime = @"image/jpeg";
	else if ([ext isEqualToString:@"webp"]) mime = @"image/webp";
	else if ([ext isEqualToString:@"gif"]) mime = @"image/gif";
	NSString *b64 = [d base64EncodedStringWithOptions:0];
	NSString *json = [NSString stringWithFormat:@"{\"name\":\"%@\",\"mime\":\"%@\",\"b64\":\"%@\"}",
	                  path.lastPathComponent, mime, b64];
	return strdup(json.UTF8String);
}
*/
import "C"

import "unsafe"

func pickFolder() string {
	p := C.bridgePickFolder()
	if p == nil {
		return ""
	}
	defer C.free(unsafe.Pointer(p))
	return C.GoString(p)
}

// pickImage opens the file picker for a project icon and hands back a JSON
// blob {name, mime, b64} - the same shape goClipboard answers with - or ""
// on cancel. The page downscales and data-URLs it; Go stays dumb about icons.
func pickImage() string {
	p := C.bridgePickImage()
	if p == nil {
		return ""
	}
	defer C.free(unsafe.Pointer(p))
	return C.GoString(p)
}
