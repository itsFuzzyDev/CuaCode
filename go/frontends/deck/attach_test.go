package main

import (
	"encoding/base64"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

// onePNG is the smallest thing that passes the sniff: a real 1x1 PNG.
const onePNG = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="

func writePNG(t *testing.T, dir, name string) string {
	t.Helper()
	raw, err := base64.StdEncoding.DecodeString(onePNG)
	if err != nil {
		t.Fatal(err)
	}
	path := filepath.Join(dir, name)
	if err := os.WriteFile(path, raw, 0o644); err != nil {
		t.Fatal(err)
	}
	return path
}

func TestPathToken(t *testing.T) {
	cases := []struct {
		name string
		buf  string
		want string
	}{
		{"plain", "/tmp/a.png", "/tmp/a.png"},
		{"escaped space", `/tmp/my\ shot.png`, "/tmp/my shot.png"},
		{"after words", "look at /tmp/a.png", "/tmp/a.png"},
		{"single quoted", `'/tmp/my shot.png'`, "/tmp/my shot.png"},
		{"double quoted", `"/tmp/a b.png"`, "/tmp/a b.png"},
		{"words before quote", `look at '/tmp/a b.png'`, "/tmp/a b.png"},
		{"stops at newline", "line\n/tmp/a.png", "/tmp/a.png"},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			buf := []rune(c.buf)
			got, start := pathToken(buf, len(buf))
			if got != c.want {
				t.Fatalf("path = %q, want %q", got, c.want)
			}
			if start < 0 || start > len(buf) {
				t.Fatalf("start %d out of range", start)
			}
		})
	}
}

func TestSplitEscaped(t *testing.T) {
	got := splitEscaped(`/tmp/a.png /tmp/my\ b.png '/tmp/c d.png'`)
	want := []string{"/tmp/a.png", "/tmp/my b.png", "/tmp/c d.png"}
	if len(got) != len(want) {
		t.Fatalf("got %d fields %q, want %d", len(got), got, len(want))
	}
	for i := range want {
		if got[i] != want[i] {
			t.Errorf("field %d = %q, want %q", i, got[i], want[i])
		}
	}
}

// A drop is only ever attached when the file is really there and really an
// image: a sentence that ends in ".png" is a sentence.
func TestAbsorbPath(t *testing.T) {
	dir := t.TempDir()
	path := writePNG(t, dir, "shot.png")

	m := initialModel()
	m.insert([]rune("look at " + path)...)
	took, err := m.absorbPath()
	if err != nil || !took {
		t.Fatalf("absorbPath = %v, %v; want true, nil", took, err)
	}
	if len(m.attach) != 1 || m.attach[0].Name != "shot.png" {
		t.Fatalf("attachments = %+v", m.attach)
	}
	if got := string(m.input); got != "look at " {
		t.Fatalf("input = %q, want the path taken out", got)
	}
	if m.cursor != len(m.input) {
		t.Fatalf("cursor = %d, want %d", m.cursor, len(m.input))
	}

	m2 := initialModel()
	m2.insert([]rune("the file is called shot.png")...)
	if took, err := m2.absorbPath(); took || err != nil {
		t.Fatalf("a name in prose attached itself: %v, %v", took, err)
	}
}

// Terminal.app ends a dropped path with a space, so that space is the moment
// the drop is finished — and it goes with the path rather than being left as a
// stray character in an empty message.
func TestAbsorbPathEndedBySpace(t *testing.T) {
	path := writePNG(t, t.TempDir(), "shot.png")

	m := initialModel()
	m.insert([]rune(path + " ")...)
	took, err := m.absorbEndingAt(m.cursor-1, 1)
	if err != nil || !took {
		t.Fatalf("absorbEndingAt = %v, %v; want true, nil", took, err)
	}
	if got := string(m.input); got != "" {
		t.Fatalf("input = %q, want the path and its space taken out", got)
	}
	if len(m.attach) != 1 {
		t.Fatalf("attachments = %+v", m.attach)
	}
}

// All or nothing: prose with a filename in it is prose, and stays text.
func TestAbsorbPaste(t *testing.T) {
	dir := t.TempDir()
	a := writePNG(t, dir, "a.png")
	b := writePNG(t, dir, "b.png")

	got, err := absorbPaste(a + " " + b)
	if err != nil {
		t.Fatal(err)
	}
	if len(got) != 2 || got[0].Name != "a.png" || got[1].Name != "b.png" {
		t.Fatalf("attachments = %+v", got)
	}
	if got[0].B64 != onePNG {
		t.Errorf("payload = %q, want the file's bytes base64'd", got[0].B64)
	}

	if got, err := absorbPaste("have a look at " + a); err != nil || got != nil {
		t.Fatalf("prose absorbed: %+v, %v", got, err)
	}
	if got, err := absorbPaste("nothing here"); err != nil || got != nil {
		t.Fatalf("plain text absorbed: %+v, %v", got, err)
	}
}

// The extension is a hint; the bytes decide. A .png that is not one has to be
// refused here rather than several seconds later by the provider.
func TestSniffRefusesImposters(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "not-really.png")
	if err := os.WriteFile(path, []byte("%PDF-1.4\nnope"), 0o644); err != nil {
		t.Fatal(err)
	}
	if _, err := readImage(path); err == nil {
		t.Fatal("a PDF called .png was accepted")
	} else if !strings.Contains(err.Error(), "not a png") {
		t.Fatalf("error = %v, want it to say what it is not", err)
	}
}

// A message can be nothing but pictures, and it has to draw as something.
func TestRenderUserShowsAttachments(t *testing.T) {
	m := initialModel()
	m.width, m.height = 80, 24
	rows := m.renderUser(&block{kind: kUser, files: []string{"shot.png"}})
	if len(rows) != 1 || !strings.Contains(rows[0], "shot.png") {
		t.Fatalf("rows = %q", rows)
	}

	rows = m.renderUser(&block{kind: kUser, text: "what is this", files: []string{"shot.png"}})
	if len(rows) != 2 || !strings.Contains(rows[0], "what is this") {
		t.Fatalf("rows = %q", rows)
	}
}

// The tray is a row of the input, so the input has to account for it or the
// feed and the frame disagree about how tall the screen is.
func TestTrayCountsTowardInputHeight(t *testing.T) {
	m := initialModel()
	m.width, m.height = 80, 24
	before := m.inputHeight()
	m.attach = []attachment{{Name: "a.png", Size: 1024}}
	if after := m.inputHeight(); after != before+1 {
		t.Fatalf("inputHeight = %d, want %d", after, before+1)
	}
	if got := len(m.renderInput()); got != m.inputHeight() {
		t.Fatalf("renderInput drew %d rows, inputHeight says %d", got, m.inputHeight())
	}
}
