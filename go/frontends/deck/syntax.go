package main

// Code, drawn as code.
//
// The permission prompt for a write or an edit is the one moment where what is
// on screen is not the agent talking - it is a patch you are being asked to
// approve. A patch rendered as a grey paragraph gets clicked through; a patch
// rendered as a patch gets read. So this is a small unified-diff renderer with
// a small syntax highlighter behind it: line numbers off the hunk headers, a
// tint on the lines that change, and the code inside them coloured the way the
// editor it came from would colour it.
//
// Deliberately small. It is a highlighter for eight-line hunks in a dialog, not
// a parser: one line at a time, no state carried between them, and anything it
// cannot account for is left as plain text rather than guessed at. Nothing here
// is allowed to be slow or to be wrong in a way that changes what you approve -
// the text is never altered, only coloured.

import (
	"strconv"
	"strings"
)

// langOf names the highlighter dialect for a path. The families are grouped by
// what their syntax looks like rather than by what the languages are: a
// highlighter this size cannot tell TypeScript from JavaScript and gains
// nothing from trying.
func langOf(path string) string {
	name := path
	if i := strings.LastIndexAny(name, "/\\"); i >= 0 {
		name = name[i+1:]
	}
	ext := ""
	if i := strings.LastIndex(name, "."); i > 0 {
		ext = strings.ToLower(name[i+1:])
	}

	switch ext {
	case "go":
		return "go"
	case "py", "pyi":
		return "py"
	case "js", "jsx", "ts", "tsx", "mjs", "cjs":
		return "js"
	case "rs":
		return "rs"
	case "c", "h", "cc", "cpp", "hpp", "java", "cs", "swift", "kt":
		return "c"
	case "sh", "bash", "zsh", "fish":
		return "sh"
	case "json":
		return "json"
	case "yaml", "yml", "toml":
		return "yaml"
	case "md", "markdown":
		return "md"
	}
	// Files that are known by their name rather than their extension.
	switch strings.ToLower(name) {
	case "makefile", "dockerfile", ".gitignore", ".env":
		return "sh"
	}
	return ""
}

// keywords per family. Short lists on purpose: the words that carry the shape
// of the code, not every reserved token in the grammar.
var keywords = map[string]map[string]bool{
	"go":   words("func var const type struct interface map chan package import return if else for range switch case default break continue go defer select nil true false iota error string int int64 float64 bool byte rune any make new len cap append copy delete panic recover"),
	"py":   words("def class return if elif else for while in not and or is None True False import from as with try except finally raise yield lambda pass break continue global nonlocal assert await async del self"),
	"js":   words("function const let var return if else for while of in class extends new this null undefined true false import export from default async await try catch finally throw typeof instanceof interface type enum implements public private readonly static"),
	"rs":   words("fn let mut const struct enum impl trait pub use mod match if else for while loop in return self Self Some None Ok Err true false as dyn ref move where unsafe async await"),
	"c":    words("int char void float double long short unsigned signed struct union enum typedef static const return if else for while do switch case break continue default sizeof class public private protected virtual override new delete this true false null nullptr namespace template using include define"),
	"sh":   words("if then else elif fi for while do done case esac in function return export local readonly set unset echo cd exit source alias trap shift test"),
	"yaml": words("true false null yes no on off"),
	"json": words("true false null"),
}

func words(s string) map[string]bool {
	out := map[string]bool{}
	for _, w := range strings.Fields(s) {
		out[w] = true
	}
	return out
}

// lineComment is what starts a comment that runs to the end of the line.
func lineComment(lang string) string {
	switch lang {
	case "go", "js", "rs", "c":
		return "//"
	case "py", "sh", "yaml":
		return "#"
	}
	return ""
}

// highlight splits one line of source into styled spans. Whitespace is kept
// exactly as it is: indentation is meaning, and a highlighter that normalises
// it is showing you a different file from the one being written.
func highlight(line, lang string) []span {
	if lang == "" || strings.TrimSpace(line) == "" {
		return []span{{text: line}}
	}
	if lang == "md" {
		return markdownSpans(line)
	}

	var (
		out     []span
		buf     strings.Builder
		comment = lineComment(lang)
	)
	flushWord := func() {
		if buf.Len() == 0 {
			return
		}
		word := buf.String()
		buf.Reset()
		out = append(out, span{text: word, style: wordStyle(word, lang)})
	}

	for i := 0; i < len(line); {
		c := line[i]
		switch {
		// Comments swallow the rest of the line, including anything in it that
		// would otherwise look like code.
		case comment != "" && strings.HasPrefix(line[i:], comment),
			strings.HasPrefix(line[i:], "/*"):
			flushWord()
			out = append(out, span{text: line[i:], style: sComment})
			return out

		case c == '"' || c == '\'' || c == '`':
			flushWord()
			// A docstring's opening line has no closing marker on it, and
			// tokenising it as three tiny strings is the one thing this gets
			// visibly wrong on a language it otherwise handles.
			if triple := strings.Repeat(string(c), 3); strings.HasPrefix(line[i:], triple) {
				end := len(line)
				if j := strings.Index(line[i+3:], triple); j >= 0 {
					end = i + 3 + j + 3
				}
				out = append(out, span{text: line[i:end], style: sString})
				i = end
				continue
			}
			end := closingQuote(line, i)
			out = append(out, span{text: line[i:end], style: sString})
			i = end

		case isWordByte(c):
			buf.WriteByte(c)
			i++

		default:
			flushWord()
			style := sPunct
			if c == ' ' || c == '\t' {
				style = ""
			}
			out = append(out, span{text: string(c), style: style})
			i++
		}
	}
	flushWord()
	return out
}

// wordStyle colours one bare word: a keyword, a number, a call, or nothing.
func wordStyle(word, lang string) string {
	if keywords[lang][word] {
		return sKeyword
	}
	if isNumber(word) {
		return sNumber
	}
	return ""
}

// closingQuote finds the end of a quoted run, escapes included. An unterminated
// quote runs to the end of the line rather than swallowing the next line's
// styling - half-written strings are normal in a diff.
func closingQuote(s string, start int) int {
	q := s[start]
	for i := start + 1; i < len(s); i++ {
		switch s[i] {
		case '\\':
			i++
		case q:
			return i + 1
		}
	}
	return len(s)
}

func isWordByte(c byte) bool {
	return c == '_' || c >= 'a' && c <= 'z' || c >= 'A' && c <= 'Z' || c >= '0' && c <= '9' || c == '.' && false
}

func isNumber(word string) bool {
	if word == "" {
		return false
	}
	digits := false
	for i := 0; i < len(word); i++ {
		switch c := word[i]; {
		case c >= '0' && c <= '9':
			digits = true
		case c == 'x' || c == 'X' || c >= 'a' && c <= 'f' || c >= 'A' && c <= 'F':
			// hex, but only after a digit has been seen
			if !digits {
				return false
			}
		default:
			return false
		}
	}
	return digits
}

// markdownSpans is the one non-code dialect worth a case of its own: prose
// files are written by this agent constantly, and a diff of one is unreadable
// with every line the same colour.
func markdownSpans(line string) []span {
	trimmed := strings.TrimLeft(line, " ")
	indent := line[:len(line)-len(trimmed)]

	switch {
	case strings.HasPrefix(trimmed, "#"):
		return []span{{text: line, style: sHead}}
	case strings.HasPrefix(trimmed, "> "):
		return []span{{text: indent, style: ""}, {text: trimmed, style: sComment}}
	case strings.HasPrefix(trimmed, "- "), strings.HasPrefix(trimmed, "* "), strings.HasPrefix(trimmed, "+ "):
		return []span{{text: indent, style: ""}, {text: trimmed[:2], style: sPunct}, {text: trimmed[2:], style: ""}}
	}
	return []span{{text: line}}
}

// ---------------------------------------------------------------------------
// unified diff

// diffLine is one row of a patch, already classified.
type diffLine struct {
	kind byte // '+', '-', ' ', '@', 'x' (a note the differ added)
	text string
	old  int // line number in the file as it is, 0 where there is none
	new  int // ...and as it would be
}

// parseDiff reads a unified diff into rows. The file headers go - the prompt
// already says which file this is - and the hunk headers become the line
// numbers the rows carry, which is the whole reason to read them.
func parseDiff(diff string) []diffLine {
	var (
		out            []diffLine
		oldNo, newNo   int
		started, inHnk bool
	)

	for _, raw := range strings.Split(diff, "\n") {
		line := plain(raw)
		switch {
		case strings.HasPrefix(line, "--- "), strings.HasPrefix(line, "+++ "):
			continue

		case strings.HasPrefix(line, "@@"):
			oldNo, newNo = hunkStart(line)
			inHnk = true
			// The first hunk needs no separator above it; the ones after it do,
			// because the lines between them are not in the patch at all.
			if started {
				out = append(out, diffLine{kind: '@', text: line})
			}
			started = true
			continue

		case !inHnk:
			// Anything before the first hunk that is not a header is the
			// differ talking - the cap notice, usually.
			if strings.TrimSpace(line) != "" {
				out = append(out, diffLine{kind: 'x', text: line})
			}
			continue
		}

		if line == "" {
			out = append(out, diffLine{kind: ' ', text: "", old: oldNo, new: newNo})
			oldNo, newNo = oldNo+1, newNo+1
			continue
		}

		switch body := line[1:]; line[0] {
		case '+':
			out = append(out, diffLine{kind: '+', text: body, new: newNo})
			newNo++
		case '-':
			out = append(out, diffLine{kind: '-', text: body, old: oldNo})
			oldNo++
		case ' ':
			out = append(out, diffLine{kind: ' ', text: body, old: oldNo, new: newNo})
			oldNo, newNo = oldNo+1, newNo+1
		default:
			out = append(out, diffLine{kind: 'x', text: line})
		}
	}
	return out
}

// hunkStart reads the two starting line numbers out of an @@ header.
func hunkStart(header string) (old, new int) {
	fields := strings.Fields(header)
	for _, f := range fields {
		if len(f) < 2 {
			continue
		}
		n, _ := strconv.Atoi(strings.SplitN(f[1:], ",", 2)[0])
		switch f[0] {
		case '-':
			old = n
		case '+':
			new = n
		}
	}
	return max(old, 1), max(new, 1)
}

// numCol is the width of the line-number gutter. Four digits and a space: a
// patch against a file long enough to need five is a patch nobody is reading in
// a dialog anyway, and it degrades to the last four digits rather than moving
// the column.
const numCol = 5

// diffRows renders a patch. Changed lines carry a tint across the measure, so
// the shape of the change is visible before a word of it is read; the code
// inside them is highlighted the same way whether it is arriving or leaving.
//
// Lines are truncated rather than wrapped. A re-flowed line of code is a lie
// about the code, and this one is being approved.
func diffRows(diff, lang string, width int) []string {
	lines := parseDiff(diff)
	rows := make([]string, 0, len(lines))

	for _, l := range lines {
		switch l.kind {
		case '@':
			// A gap in the file, drawn as a gap.
			rows = append(rows, margin+"  "+paint(cRule, strings.Repeat("╌", max(width-2, 4))))
			continue
		case 'x':
			rows = append(rows, margin+"  "+paint(cGhost, trunc(l.text, width-2)))
			continue
		}

		// Changed lines are marked in the gutter and lit in the text; context is
		// the same code one step quieter. No panels of colour behind the words:
		// a block of green under code is louder than the code, and the code is
		// the thing being approved.
		mark, markStyle, base, no := " ", cRule, cMuted, l.new
		switch l.kind {
		case '+':
			mark, markStyle, base = "+", cOK, cInk
		case '-':
			mark, markStyle, base, no = "-", cErr, cInk, l.old
		}

		gutter := paint(cGhost, padLeft(shortNum(no), numCol)) +
			paint(markStyle, " "+bar(l.kind)+mark+" ")
		code := renderSpans(truncSpans(highlight(l.text, lang), max(width-numCol-6, 8)), base)
		rows = append(rows, margin+"  "+gutter+code)
	}
	return rows
}

// bar is the rule down the left of the patch: solid beside a line that changes,
// hairline beside one that is only there for context. It is what gives a hunk
// its shape now that nothing is painted behind it.
func bar(kind byte) string {
	if kind == '+' || kind == '-' {
		return "▌"
	}
	return "│"
}

// shortNum keeps a line number inside the gutter by dropping its leading digits
// rather than widening the column.
func shortNum(n int) string {
	if n <= 0 {
		return ""
	}
	s := strconv.Itoa(n)
	if len(s) > numCol-1 {
		s = s[len(s)-(numCol-1):]
	}
	return s
}

// truncSpans cuts a styled line to a width, keeping the styling of whatever
// survives and marking the cut.
func truncSpans(line []span, w int) []span {
	if spanWidth(line) <= w {
		return line
	}
	out, used := make([]span, 0, len(line)+1), 0
	for _, sp := range line {
		room := w - 1 - used
		if room <= 0 {
			break
		}
		if vw(sp.text) <= room {
			out, used = append(out, sp), used+vw(sp.text)
			continue
		}
		head := trimToWidth(sp.text, room)
		out = append(out, span{text: head, style: sp.style})
		break
	}
	return append(out, span{text: "…", style: cGhost})
}

// contentRows draws a file that does not exist yet: every line of it is
// arriving, so it is drawn as a patch that is all additions rather than as a
// wall of text with no shape.
func contentRows(content, lang string, width int) []string {
	body := strings.Split(strings.TrimRight(plain(content), "\n"), "\n")
	rows := make([]string, 0, len(body))
	for i, line := range body {
		gutter := paint(cGhost, padLeft(shortNum(i+1), numCol)) + paint(cOK, " ▌+ ")
		code := renderSpans(truncSpans(highlight(line, lang), max(width-numCol-6, 8)), cInk)
		rows = append(rows, margin+"  "+gutter+code)
	}
	return rows
}

// codeRows draws a file's own text as code: a line-number gutter, the syntax
// coloured, and no patch markers, because nothing here is changing. It is what
// a read comes back as, and what the halves of an edit are shown as.
//
// A read hands its lines back numbered - "12\tfunc main() {" - because the
// model needs the numbers to edit by them. Those numbers are the gutter here
// rather than the first word of the code; text with none is numbered from one.
func codeRows(content, lang string, width int) []string {
	lines := strings.Split(strings.TrimRight(content, "\n"), "\n")
	rows := make([]string, 0, len(lines))

	for i, raw := range lines {
		no, text := i+1, raw
		if n, body, is := numberedLine(raw); is {
			no, text = n, body
		}
		gutter := paint(cGhost, padLeft(shortNum(no), numCol)) + paint(cRule, " │ ")
		code := renderSpans(truncSpans(highlight(plain(text), lang), max(width-numCol-6, 8)), cInk)
		rows = append(rows, margin+"  "+gutter+code)
	}
	return rows
}

// numberedLine splits a read's "<n>\t<text>" line into the two.
func numberedLine(line string) (int, string, bool) {
	tab := strings.IndexByte(line, '\t')
	if tab <= 0 {
		return 0, line, false
	}
	n, err := strconv.Atoi(line[:tab])
	if err != nil || n <= 0 {
		return 0, line, false
	}
	return n, line[tab+1:], true
}

// numbered reports whether a string is a read's numbered lines rather than
// ordinary prose. It is what lets content be drawn as code for a file whose
// extension says nothing - a Makefile, a log, a file with no suffix at all -
// without a web page's paragraphs being given line numbers they never had.
func numbered(s string) bool {
	line := s
	if i := strings.IndexByte(s, '\n'); i >= 0 {
		line = s[:i]
	}
	_, _, is := numberedLine(line)
	return is
}
