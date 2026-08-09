package main

import (
	"testing"

	"gioui.org/font"
)

// TestCollection guards the embed: a renamed or missing .ttf would otherwise
// only show up as the UI silently falling back to Go fonts.
func TestCollection(t *testing.T) {
	faces, mono := collection()
	if mono != FaceMono {
		t.Errorf("mono typeface = %q, want %q", mono, FaceMono)
	}

	want := []font.Font{
		{Typeface: FaceMono, Weight: font.Normal, Style: font.Regular},
		{Typeface: FaceMono, Weight: font.Bold, Style: font.Regular},
		{Typeface: FaceMono, Weight: font.Normal, Style: font.Italic},
		{Typeface: FaceUI, Weight: font.Normal, Style: font.Regular},
		{Typeface: FaceUI, Weight: font.Medium, Style: font.Regular},
		{Typeface: FaceUI, Weight: font.SemiBold, Style: font.Regular},
	}

	for _, w := range want {
		if !hasFace(faces, w) {
			t.Errorf("missing face %v %v %v", w.Typeface, w.Weight, w.Style)
		}
	}
}

func hasFace(faces []font.FontFace, want font.Font) bool {
	for _, f := range faces {
		if f.Font.Typeface == want.Typeface && f.Font.Weight == want.Weight && f.Font.Style == want.Style {
			return true
		}
	}
	return false
}
