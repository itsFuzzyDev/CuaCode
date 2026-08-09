package main

import (
	"embed"
	"os"

	"gioui.org/font"
	"gioui.org/font/gofont"
	"gioui.org/font/opentype"
)

// Two faces, one job each. Cascadia Code carries everything that came off the
// wire — log rows and the input — because the payloads are JSON and want a
// mono grid. Inter carries the chrome the worker never sent: state, counters,
// key hints. Both are humanist, both have a tall x-height, so the small caps
// sit level with the code beside them.
const (
	FaceMono font.Typeface = "Cascadia Code"
	FaceUI   font.Typeface = "Inter"
)

// FontEnv names a .ttf/.ttc to use for the mono face instead of the bundled
// Cascadia Code, so the console can be re-faced without a rebuild.
const FontEnv = "CUACODE_FONT"

//go:embed fonts/*.ttf
var fontFS embed.FS

// collection builds the shaper's font set: the bundled faces first, then Go
// fonts as a glyph fallback for anything the others don't cover. It also
// reports the mono typeface to ask for, which $CUACODE_FONT can replace.
func collection() ([]font.FontFace, font.Typeface) {
	mono := FaceMono
	faces := make([]font.FontFace, 0, 8)

	if override, name := loadFile(os.Getenv(FontEnv)); len(override) > 0 {
		faces, mono = append(faces, override...), name
	}

	for _, name := range []string{
		"fonts/CascadiaCode-Regular.ttf",
		"fonts/CascadiaCode-Bold.ttf",
		"fonts/CascadiaCode-Italic.ttf",
		"fonts/Inter-Regular.ttf",
		"fonts/Inter-Medium.ttf",
		"fonts/Inter-SemiBold.ttf",
	} {
		src, err := fontFS.ReadFile(name)
		if err != nil {
			continue // a face that failed to embed simply falls back
		}
		if parsed, err := opentype.ParseCollection(src); err == nil {
			faces = append(faces, parsed...)
		}
	}

	return append(faces, gofont.Collection()...), mono
}

// loadFile parses a font off disk, returning its faces and the typeface name
// to address them by. A missing or unreadable path yields nothing, leaving the
// bundled face in place.
func loadFile(path string) ([]font.FontFace, font.Typeface) {
	if path == "" {
		return nil, ""
	}
	src, err := os.ReadFile(path)
	if err != nil {
		return nil, ""
	}
	faces, err := opentype.ParseCollection(src)
	if err != nil || len(faces) == 0 {
		return nil, ""
	}
	return faces, faces[0].Font.Typeface
}
