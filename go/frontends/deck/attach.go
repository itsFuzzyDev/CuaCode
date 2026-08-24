package main

// The two doors a picture gets into a terminal through.
//
// A terminal cannot receive an image. What it can receive is a path — every
// mainstream terminal turns a file dropped on its window into the file's path,
// typed or pasted into whatever was at the cursor — and a keystroke, which is
// how the clipboard gets read: the system clipboard holds the bytes, and the
// terminal is not in that conversation at all.
//
// So: absorbPath watches the buffer for a path to an image that exists and
// quietly takes it out of the text, and ctrl+v asks core/attach for whatever
// the clipboard is holding. Reading a file, and reading the clipboard, are
// both over there — the window does them too, and has to get the same answer.

import (
	"os"
	"path/filepath"
	"strings"

	"cuacode/core/attach"
	"cuacode/core/protocol"
)

// attachment, and the reading of one, live in core/attach: the window needs
// exactly the same answers about a file as the terminal does.
type attachment = attach.Image

var (
	fmtBytes       = attach.FmtBytes
	looksLikeImage = attach.LooksLikeImage
	readImage      = attach.ReadFile
	readClipboard  = attach.Clipboard
)

// wire turns the pending attachments into what SendChatWith takes.
func wire(atts []attachment) []protocol.Image {
	if len(atts) == 0 {
		return nil
	}
	out := make([]protocol.Image, 0, len(atts))
	for _, a := range atts {
		out = append(out, protocol.Image{Name: a.Name, B64: a.B64})
	}
	return out
}

// ---------------------------------------------------------------- the paths

// pathToken finds the path ending at the cursor, walking back over the two
// ways a terminal hands one over: backslash-escaped spaces (Terminal.app,
// iTerm2, Ghostty) and a quoted string (WezTerm, and anything pasting a shell
// word). It returns the unescaped path and where in the buffer it started.
//
// An empty path means there is nothing there worth a stat.
func pathToken(buf []rune, at int) (path string, start int) {
	if at <= 0 {
		return "", at
	}
	// A quoted path is unambiguous: find its opening quote and take the lot.
	if q := buf[at-1]; q == '\'' || q == '"' {
		for i := at - 2; i >= 0; i-- {
			if buf[i] == q {
				return string(buf[i+1 : at-1]), i
			}
		}
		return "", at
	}

	i := at
	for i > 0 {
		// A space is a boundary unless the character before it escapes it.
		if buf[i-1] == ' ' && (i < 2 || buf[i-2] != '\\') {
			break
		}
		if buf[i-1] == '\n' {
			break
		}
		i--
	}
	return unescape(string(buf[i:at])), i
}

// unescape undoes the backslashes a terminal puts in front of the characters a
// shell would otherwise split on. Only those: a backslash in front of anything
// else is part of the name on the one OS where backslashes are separators.
func unescape(s string) string {
	var b strings.Builder
	b.Grow(len(s))
	for i := 0; i < len(s); i++ {
		if s[i] == '\\' && i+1 < len(s) && strings.ContainsRune(" '\"()&;", rune(s[i+1])) {
			i++
		}
		b.WriteByte(s[i])
	}
	return b.String()
}

// expand resolves the two prefixes a dropped path can arrive with: a file URL,
// which is what some file managers hand over, and ~ for the home directory.
func expand(path string) string {
	if rest, ok := strings.CutPrefix(path, "file://"); ok {
		// A file URL from a drop is host-less ("file:///Users/…"); anything
		// before the first slash is a host we cannot read from anyway.
		if i := strings.Index(rest, "/"); i >= 0 {
			path = rest[i:]
		}
	}
	if path == "~" || strings.HasPrefix(path, "~/") {
		if home, err := os.UserHomeDir(); err == nil {
			path = filepath.Join(home, strings.TrimPrefix(path[1:], "/"))
		}
	}
	return path
}

// absorbPath takes an image path out of the buffer and attaches it instead.
//
// This is the drop: the terminal has just typed or pasted a path where the
// cursor was, and the only way to know it happened is to look at what is now
// sitting there. Cheap because the extension is checked before the disk is,
// and it runs where a path can have just ended — after a paste, and after a
// keystroke that could have finished one.
//
// Reports whether anything was taken, and any reason a path that plainly was
// one could not be.
func (m *model) absorbPath() (bool, error) { return m.absorbEndingAt(m.cursor, 0) }

// absorbEndingAt is absorbPath aimed somewhere other than the cursor, and told
// how many characters after the path to take with it.
//
// That is the separator a terminal types after a drop: Terminal.app ends the
// path it typed with a space, so the moment the path is finished is the moment
// a space arrives, and the space is part of the gesture rather than part of the
// message.
func (m *model) absorbEndingAt(end, trail int) (bool, error) {
	if end < 0 || end > len(m.input) || end+trail > len(m.input) {
		return false, nil
	}
	raw, start := pathToken(m.input, end)
	if raw == "" {
		return false, nil
	}
	path := expand(strings.TrimSpace(raw))
	if !looksLikeImage(path) {
		return false, nil
	}
	att, err := readImage(path)
	if err != nil {
		// Only worth reporting for something that really was a path to a file:
		// a sentence that happens to end in ".png" is not a failed attachment.
		if _, statErr := os.Stat(path); statErr != nil {
			return false, nil
		}
		return false, err
	}

	m.attach = append(m.attach, att)
	// The path is taken out of the message. It was never text the user meant to
	// send — it is the terminal's way of describing a drop — and leaving it
	// behind would send the model a filename beside a picture of the file.
	m.input = append(m.input[:start], m.input[end+trail:]...)
	m.cursor = start
	return true, nil
}

// absorbPaste attaches every path in a pasted block, for the drop of several
// files at once: terminals separate them with spaces, escaped exactly as a
// single one is.
//
// All or nothing. A paste that is mostly prose with a filename in it is prose,
// and pulling one word out of it would be the wrong reading of an ordinary
// paste.
func absorbPaste(s string) ([]attachment, error) {
	fields := splitEscaped(strings.TrimSpace(s))
	if len(fields) == 0 {
		return nil, nil
	}
	for _, f := range fields {
		if !looksLikeImage(expand(f)) {
			return nil, nil
		}
	}
	var out []attachment
	for _, f := range fields {
		att, err := readImage(expand(f))
		if err != nil {
			return nil, err
		}
		out = append(out, att)
	}
	return out, nil
}

// splitEscaped breaks a line on the spaces that separate arguments, keeping
// the ones a backslash or a pair of quotes protects. The same reading a shell
// gives it, which is the reading the terminal wrote it in.
func splitEscaped(s string) []string {
	var (
		out   []string
		cur   strings.Builder
		quote rune
	)
	flush := func() {
		if cur.Len() > 0 {
			out = append(out, cur.String())
			cur.Reset()
		}
	}
	rs := []rune(s)
	for i := 0; i < len(rs); i++ {
		switch r := rs[i]; {
		case quote != 0 && r == quote:
			quote = 0
		case quote != 0:
			cur.WriteRune(r)
		case r == '\'' || r == '"':
			quote = r
		case r == '\\' && i+1 < len(rs):
			i++
			cur.WriteRune(rs[i])
		case r == ' ' || r == '\t' || r == '\n' || r == '\r':
			flush()
		default:
			cur.WriteRune(r)
		}
	}
	flush()
	return out
}
