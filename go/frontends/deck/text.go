package main

// Inline formatting for the model's prose: a deliberately small subset of
// markdown - fenced code, inline code, bold, italic, headings, bullets,
// quotes.
//
// Everything is parsed into styled spans and wrapped by the width of the text
// alone, so styling can never move a column. That is the whole reason this
// exists rather than colouring strings after wrapping them: the markers are
// gone by the time anything is measured.

import "strings"

// span is a run of text under one style.
type span struct {
	text  string
	style string
}

// atom is a wrappable unit: one word, plus whether a space separates it from
// the previous one.
type atom struct {
	text  string
	style string
	space bool
}

// formatProse turns a block of prose into wrapped, styled lines.
func formatProse(text string, w int) [][]span {
	if w < 4 {
		w = 4
	}

	var out [][]span
	code := false

	for _, line := range strings.Split(text, "\n") {
		if strings.HasPrefix(strings.TrimSpace(line), "```") {
			code = !code
			continue
		}

		if code {
			// Code keeps its own whitespace, so it is truncated rather than
			// wrapped - a re-flowed line of code is a lie about the code.
			out = append(out, []span{{text: trunc(" "+line, w), style: sCode}})
			continue
		}

		if strings.TrimSpace(line) == "" {
			out = append(out, nil)
			continue
		}

		body, prefix, hang, base := blockLead(line)
		lead := 0
		if prefix != nil {
			lead = vw(prefix.text)
		}
		wrapped := wrapAtoms(splitAtoms(parseInline(body)), w, lead, hang)
		for i, l := range wrapped {
			if base != "" {
				for j := range l {
					if l[j].style == "" {
						l[j].style = base
					}
				}
			}
			switch {
			case i == 0 && prefix != nil:
				out = append(out, append([]span{*prefix}, l...))
			case hang > 0:
				out = append(out, append([]span{{text: strings.Repeat(" ", hang)}}, l...))
			default:
				out = append(out, l)
			}
		}
	}
	return out
}

// blockLead strips a line's block marker, returning the remaining text, the
// span that replaces the marker, the indent its continuation lines get, and a
// style for the whole line.
func blockLead(line string) (body string, prefix *span, hang int, base string) {
	trimmed := strings.TrimLeft(line, " ")
	indent := len(line) - len(trimmed)

	switch {
	case strings.HasPrefix(trimmed, "#"):
		if h := strings.TrimLeft(trimmed, "#"); strings.HasPrefix(h, " ") {
			return strings.TrimSpace(h), nil, 0, sHead
		}

	case strings.HasPrefix(trimmed, "> "):
		return trimmed[2:], &span{text: "│ ", style: cFaint}, 2, cMuted

	case strings.HasPrefix(trimmed, "- "), strings.HasPrefix(trimmed, "* "), strings.HasPrefix(trimmed, "+ "):
		pad := strings.Repeat(" ", indent)
		return trimmed[2:], &span{text: pad + "• ", style: cFaint}, indent + 2, ""
	}
	return line, nil, 0, ""
}

// parseInline splits a line on `code`, **bold** and *italic*. An unclosed
// marker is left as literal text - half-typed emphasis is normal while a
// response is still streaming, and it must not restyle the rest of the line.
func parseInline(s string) []span {
	var (
		out  []span
		buf  strings.Builder
		base = ""
	)
	flush := func() {
		if buf.Len() > 0 {
			out = append(out, span{text: buf.String(), style: base})
			buf.Reset()
		}
	}

	for i := 0; i < len(s); {
		switch {
		case s[i] == '`':
			if j := strings.IndexByte(s[i+1:], '`'); j >= 0 {
				flush()
				out = append(out, span{text: s[i+1 : i+1+j], style: sCode})
				i += j + 2
				continue
			}

		case strings.HasPrefix(s[i:], "**"):
			if j := strings.Index(s[i+2:], "**"); j >= 0 {
				flush()
				out = append(out, span{text: s[i+2 : i+2+j], style: sBold})
				i += j + 4
				continue
			}

		case s[i] == '*':
			if j := strings.IndexByte(s[i+1:], '*'); j > 0 {
				flush()
				out = append(out, span{text: s[i+1 : i+1+j], style: sItalic})
				i += j + 2
				continue
			}
		}
		buf.WriteByte(s[i])
		i++
	}

	flush()
	return out
}

// splitAtoms breaks spans at spaces so the wrapper has something to break on.
func splitAtoms(spans []span) []atom {
	var out []atom
	pending := false

	for _, sp := range spans {
		for i, word := range strings.Split(sp.text, " ") {
			if i > 0 {
				pending = true
			}
			if word == "" {
				continue
			}
			out = append(out, atom{text: word, style: sp.style, space: pending && len(out) > 0})
			pending = false
		}
	}
	return out
}

// wrapAtoms greedily fills lines of width w. The first line gives up lead cells
// to the marker that will be prepended to it; every line after gives up hang to
// the indent that keeps it under the first.
func wrapAtoms(atoms []atom, w, lead, hang int) [][]span {
	var (
		out  [][]span
		line []span
		used int
	)
	limit := func() int {
		if len(out) == 0 {
			return max(w-lead, 1)
		}
		return max(w-hang, 1)
	}

	for _, a := range atoms {
		word := a.text
		for {
			gap := 0
			if a.space && used > 0 {
				gap = 1
			}
			if room := limit() - used - gap; vw(word) > room {
				if used > 0 {
					out, line, used = append(out, line), nil, 0
					a.space = false
					continue
				}
				// A single word longer than the line: split it rather than
				// overflow, because overflow is what breaks the frame.
				head := trimToWidth(word, limit())
				out = append(out, []span{{text: head, style: a.style}})
				word = word[len(head):]
				if word == "" {
					break
				}
				continue
			}
			if gap > 0 {
				line = append(line, span{text: " "})
				used++
			}
			line = append(line, span{text: word, style: a.style})
			used += vw(word)
			break
		}
	}

	if len(line) > 0 {
		out = append(out, line)
	}
	return out
}

// trimToWidth returns the longest prefix of s that fits in w cells.
func trimToWidth(s string, w int) string {
	if w < 1 {
		w = 1
	}
	used, end := 0, 0
	for i, r := range s {
		rw := vw(string(r))
		if used+rw > w {
			break
		}
		used, end = used+rw, i+len(string(r))
	}
	if end == 0 {
		_, size := decodeRune(s)
		end = size
	}
	return s[:end]
}

func decodeRune(s string) (rune, int) {
	for i, r := range s {
		_ = i
		return r, len(string(r))
	}
	return 0, 0
}

// spanWidth is the visible width of a line of spans.
func spanWidth(line []span) int {
	w := 0
	for _, sp := range line {
		w += vw(sp.text)
	}
	return w
}

// renderSpans paints a line, falling back to base wherever a span has no style
// of its own.
func renderSpans(line []span, base string) string {
	var b strings.Builder
	for _, sp := range line {
		style := sp.style
		if style == "" {
			style = base
		}
		b.WriteString(paint(style, sp.text))
	}
	return b.String()
}
