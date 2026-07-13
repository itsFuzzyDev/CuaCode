package main

import (
	"bytes"
	"encoding/json"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"reflect"
	"runtime"
	"strconv"
	"strings"

	"tui/handler/protocol"

	tea "github.com/charmbracelet/bubbletea"
	"github.com/charmbracelet/x/cellbuf"
)

const (
	reverseOn  = "\x1b[7m"
	reverseOff = "\x1b[0m"
)

type shiftEnterMsg struct{}

type model struct {
	worker     *protocol.Worker
	content    []string
	wrapped    []string
	inputRunes []rune
	cursorPos  int // rune position in flat input
	scroll     int // wrapped lines scrolled up from bottom
	width      int
	height     int
	msgID      int
}

func initialModel() *model {
	return &model{
		content: make([]string, 0),
		wrapped: make([]string, 0),
	}
}

func (m *model) contentHeight() int {
	inputView := m.buildInputView()
	inputH := len(inputView.lines)
	if inputH < 1 {
		inputH = 1
	}
	h := m.height - inputH - 1 // splitter
	if h < 1 {
		h = 1
	}
	return h
}

func (m *model) rebuildWrapped() {
	m.wrapped = make([]string, 0, len(m.content)*2)
	for _, line := range m.content {
		if m.width > 0 {
			w := cellbuf.Wrap(line, m.width, "")
			m.wrapped = append(m.wrapped, strings.Split(w, "\n")...)
		} else {
			m.wrapped = append(m.wrapped, line)
		}
	}
	maxScroll := len(m.wrapped) - m.contentHeight()
	if maxScroll < 0 {
		maxScroll = 0
	}
	if m.scroll > maxScroll {
		m.scroll = maxScroll
	}
}

func (m *model) appendContent(line string) {
	m.content = append(m.content, line)
	var newWrapped []string
	if m.width > 0 {
		w := cellbuf.Wrap(line, m.width, "")
		newWrapped = strings.Split(w, "\n")
	} else {
		newWrapped = []string{line}
	}
	m.wrapped = append(m.wrapped, newWrapped...)
	if m.scroll < 5 {
		m.scroll = 0
		return
	}
	m.scroll += len(newWrapped)
	maxScroll := len(m.wrapped) - m.contentHeight()
	if maxScroll < 0 {
		maxScroll = 0
	}
	if m.scroll > maxScroll {
		m.scroll = maxScroll
	}
}

func (m *model) scrollUp(n int) {
	m.scroll += n
	maxScroll := len(m.wrapped) - m.contentHeight()
	if maxScroll < 0 {
		maxScroll = 0
	}
	if m.scroll > maxScroll {
		m.scroll = maxScroll
	}
	if m.scroll < 0 {
		m.scroll = 0
	}
}

func (m *model) scrollDown(n int) {
	m.scroll -= n
	if m.scroll < 0 {
		m.scroll = 0
	}
}

func getFrontmostApp() string {
	switch runtime.GOOS {
	case "darwin":
		out, err := exec.Command("osascript", "-e",
			`tell application "System Events" to get name of first process whose frontmost is true`).Output()
		if err == nil {
			return strings.TrimSpace(string(out))
		}
	case "windows":
		ps := `Add-Type -MemberDefinition '[DllImport("user32.dll")]public static extern IntPtr GetForegroundWindow();' -Name NativeMethods -PassThru | ForEach-Object { $hwnd = $_.GetForegroundWindow(); (Get-Process | Where-Object { $_.MainWindowHandle -eq $hwnd }).ProcessName }`
		out, err := exec.Command("powershell", "-NoProfile", "-Command", ps).Output()
		if err == nil {
			return strings.TrimSpace(string(out))
		}
	default: // linux
		out, err := exec.Command("xdotool", "getactivewindow", "getwindowname").Output()
		if err == nil {
			return strings.TrimSpace(string(out))
		}
	}
	return ""
}

func getTerminalInfo() protocol.TerminalData {
	t := protocol.TerminalData{
		TERM:         os.Getenv("TERM"),
		FrontmostApp: getFrontmostApp(),
	}
	if p := os.Getenv("TERM_PROGRAM"); p != "" {
		t.Program = p
	}
	if out, err := exec.Command("tty").Output(); err == nil {
		t.TTY = strings.TrimSpace(string(out))
	}
	return t
}

func (m *model) sendChat(text string) {
	m.msgID++
	data, _ := json.Marshal(protocol.CmdData{Action: "chat", Text: text})
	env := protocol.Envelope{
		Type: "cmd",
		ID:   fmt.Sprintf("msg-%d", m.msgID),
		Data: data,
	}
	m.worker.SendEnv(env)
	m.appendContent("> " + text)
}

func (m *model) deleteWordBackward() int {
	if m.cursorPos <= 0 {
		return 0
	}
	pos := m.cursorPos - 1
	for pos >= 0 && m.inputRunes[pos] == ' ' {
		pos--
	}
	for pos >= 0 && m.inputRunes[pos] != ' ' && m.inputRunes[pos] != '\n' {
		pos--
	}
	return pos + 1
}

func (m *model) moveCursorLineUp() {
	if m.cursorPos == 0 {
		return
	}
	newlinePos := -1
	for i := m.cursorPos - 1; i >= 0; i-- {
		if m.inputRunes[i] == '\n' {
			newlinePos = i
			break
		}
	}
	if newlinePos == -1 {
		return
	}
	curLineStart := newlinePos + 1
	col := m.cursorPos - curLineStart

	prevNewlinePos := -1
	for i := newlinePos - 1; i >= 0; i-- {
		if m.inputRunes[i] == '\n' {
			prevNewlinePos = i
			break
		}
	}
	prevLineStart := prevNewlinePos + 1
	prevLineEnd := newlinePos

	target := prevLineStart + col
	if target > prevLineEnd {
		target = prevLineEnd
	}
	m.cursorPos = target
}

func (m *model) moveCursorLineDown() {
	nextNewlinePos := -1
	for i := m.cursorPos; i < len(m.inputRunes); i++ {
		if m.inputRunes[i] == '\n' {
			nextNewlinePos = i
			break
		}
	}
	if nextNewlinePos == -1 {
		return
	}
	curLineStart := 0
	for i := m.cursorPos - 1; i >= 0; i-- {
		if m.inputRunes[i] == '\n' {
			curLineStart = i + 1
			break
		}
	}
	col := m.cursorPos - curLineStart

	nextLineStart := nextNewlinePos + 1
	nextLineEnd := len(m.inputRunes)
	for i := nextLineStart; i < len(m.inputRunes); i++ {
		if m.inputRunes[i] == '\n' {
			nextLineEnd = i
			break
		}
	}

	target := nextLineStart + col
	if target > nextLineEnd {
		target = nextLineEnd
	}
	m.cursorPos = target
}

type inputView struct {
	lines      []string
	cursorLine int
	cursorCol  int
}

func (m *model) buildInputView() inputView {
	prompt := "> "
	avail := m.width - len(prompt)
	if avail < 1 {
		avail = 1
	}

	v := inputView{cursorLine: -1, cursorCol: 0}
	cursorPos := m.cursorPos
	logicalLines := strings.Split(string(m.inputRunes), "\n")

	runeOffset := 0
	for li, line := range logicalLines {
		prefix := strings.Repeat(" ", len(prompt))
		if li == 0 {
			prefix = prompt
		}

		var wrappedLines []string
		if len(line) == 0 {
			wrappedLines = []string{""}
		} else {
			w := cellbuf.Wrap(line, avail, "")
			wrappedLines = strings.Split(w, "\n")
		}

		for _, wl := range wrappedLines {
			wlRunes := []rune(wl)
			lineStart := runeOffset
			lineEnd := runeOffset + len(wlRunes)

			if cursorPos >= lineStart && cursorPos <= lineEnd {
				v.cursorLine = len(v.lines)
				v.cursorCol = len([]rune(prefix)) + (cursorPos - lineStart)
			}

			v.lines = append(v.lines, prefix+wl)
			runeOffset = lineEnd
		}

		if li < len(logicalLines)-1 {
			runeOffset++ // \n
		}
	}

	if len(v.lines) == 0 {
		v.lines = []string{prompt}
		v.cursorLine = 0
		v.cursorCol = len(prompt)
	} else if cursorPos >= runeOffset {
		v.cursorLine = len(v.lines) - 1
		v.cursorCol = len([]rune(v.lines[len(v.lines)-1]))
	}

	return v
}

func (m *model) renderInputWithCursor(v inputView) []string {
	if v.cursorLine < 0 || v.cursorLine >= len(v.lines) {
		return v.lines
	}

	lines := make([]string, len(v.lines))
	copy(lines, v.lines)

	line := []rune(lines[v.cursorLine])
	col := v.cursorCol

	if m.cursorPos >= len(m.inputRunes) {
		lines[v.cursorLine] = string(line) + reverseOn + " " + reverseOff
	} else if col < len(line) {
		before := string(line[:col])
		at := string(line[col])
		after := ""
		if col+1 < len(line) {
			after = string(line[col+1:])
		}
		lines[v.cursorLine] = before + reverseOn + at + reverseOff + after
	} else {
		lines[v.cursorLine] = string(line) + reverseOn + " " + reverseOff
	}

	return lines
}

func (m *model) hasMultipleInputLines() bool {
	for _, r := range m.inputRunes {
		if r == '\n' {
			return true
		}
	}
	return false
}

func (m *model) Init() tea.Cmd {
	return tea.Sequence(
		tea.HideCursor,
		func() tea.Msg {
			fmt.Print("\x1b[>1u")
			return nil
		},
	)
}

func (m *model) Update(msg tea.Msg) (tea.Model, tea.Cmd) {
	switch msg := msg.(type) {
	case tea.WindowSizeMsg:
		m.width = msg.Width
		m.height = msg.Height
		m.rebuildWrapped()

	case tea.MouseMsg:
		switch msg.Button {
		case tea.MouseButtonWheelUp:
			m.scrollUp(1)
		case tea.MouseButtonWheelDown:
			m.scrollDown(1)
		}

	case shiftEnterMsg:
		m.inputRunes = append(m.inputRunes[:m.cursorPos], append([]rune{'\n'}, m.inputRunes[m.cursorPos:]...)...)
		m.cursorPos++

	case tea.KeyMsg:
		switch msg.Type {
		case tea.KeyCtrlC:
			if m.worker != nil {
				quitData, _ := json.Marshal(protocol.CmdData{Action: "stop"})
				m.worker.SendEnv(protocol.Envelope{Type: "cmd", ID: "quit", Data: quitData})
			}
			return m, tea.Sequence(
				func() tea.Msg {
					fmt.Print("\x1b[<1u\x1b[?25h")
					return nil
				},
				tea.Quit,
			)
		case tea.KeyEnter:
			if msg.Alt {
				m.inputRunes = append(m.inputRunes[:m.cursorPos], append([]rune{'\n'}, m.inputRunes[m.cursorPos:]...)...)
				m.cursorPos++
			} else if len(m.inputRunes) > 0 {
				m.sendChat(string(m.inputRunes))
				m.inputRunes = m.inputRunes[:0]
				m.cursorPos = 0
			}
		case tea.KeySpace:
			m.inputRunes = append(m.inputRunes[:m.cursorPos], append([]rune{' '}, m.inputRunes[m.cursorPos:]...)...)
			m.cursorPos++
		case tea.KeyTab:
			m.inputRunes = append(m.inputRunes[:m.cursorPos], append([]rune{'\t'}, m.inputRunes[m.cursorPos:]...)...)
			m.cursorPos++
		case tea.KeyShiftTab:
			m.inputRunes = append(m.inputRunes[:m.cursorPos], append([]rune{'\t'}, m.inputRunes[m.cursorPos:]...)...)
			m.cursorPos++
		case tea.KeyBackspace:
			if m.cursorPos > 0 {
				m.inputRunes = append(m.inputRunes[:m.cursorPos-1], m.inputRunes[m.cursorPos:]...)
				m.cursorPos--
			}
		case tea.KeyDelete:
			if m.cursorPos < len(m.inputRunes) {
				m.inputRunes = append(m.inputRunes[:m.cursorPos], m.inputRunes[m.cursorPos+1:]...)
			}
		case tea.KeyCtrlU:
			m.inputRunes = m.inputRunes[m.cursorPos:]
			m.cursorPos = 0
		case tea.KeyCtrlW:
			m.cursorPos = m.deleteWordBackward()
			m.inputRunes = m.inputRunes[m.cursorPos:]
		case tea.KeyLeft:
			if m.cursorPos > 0 {
				m.cursorPos--
			}
		case tea.KeyRight:
			if m.cursorPos < len(m.inputRunes) {
				m.cursorPos++
			}
		case tea.KeyUp:
			if m.hasMultipleInputLines() {
				m.moveCursorLineUp()
			} else {
				m.scrollUp(1)
			}
		case tea.KeyDown:
			if m.hasMultipleInputLines() {
				m.moveCursorLineDown()
			} else {
				m.scrollDown(1)
			}
		case tea.KeyPgUp:
			m.scrollUp(m.contentHeight())
		case tea.KeyPgDown:
			m.scrollDown(m.contentHeight())
		case tea.KeyHome:
			m.cursorPos = 0
		case tea.KeyEnd:
			m.cursorPos = len(m.inputRunes)
		case tea.KeyRunes:
			for _, r := range msg.Runes {
				m.inputRunes = append(m.inputRunes[:m.cursorPos], append([]rune{r}, m.inputRunes[m.cursorPos:]...)...)
				m.cursorPos++
			}
		}

	case protocol.EventMsg:
		var env protocol.Envelope
		if err := json.Unmarshal(msg.Raw, &env); err != nil {
			m.appendContent(fmt.Sprintf("[bad json] %s", msg.Raw))
		} else {
			if env.Type == "status" && (string(env.Data) == `{"state":"ready"}` || string(env.Data) == `{"state":"startup"}`) && m.worker != nil {
				termData, _ := json.Marshal(getTerminalInfo())
				reply := protocol.Envelope{Type: "terminal", ID: env.ID, Data: termData}
				m.worker.SendEnv(reply)
			}
			m.appendContent(fmt.Sprintf("[%s|%s] %s", env.Type, env.ID, string(env.Data)))
		}
	}
	return m, nil
}

func (m *model) View() string {
	if m.width == 0 || m.height == 0 {
		return "loading..."
	}
	if m.height < 3 {
		return "terminal too small\n"
	}

	var b strings.Builder

	inputView := m.buildInputView()
	inputLines := m.renderInputWithCursor(inputView)
	inputH := len(inputLines)
	if inputH < 1 {
		inputH = 1
	}

	contentH := m.height - inputH - 1 // splitter
	if contentH < 1 {
		contentH = 1
	}

	total := len(m.wrapped)
	start := total - contentH - m.scroll
	if start < 0 {
		start = 0
	}
	end := start + contentH
	if end > total {
		end = total
	}

	for i := 0; i < contentH; i++ {
		idx := start + i
		if idx >= 0 && idx < total {
			b.WriteString(m.wrapped[idx])
		}
		if i < contentH-1 {
			b.WriteByte('\n')
		}
	}

	// Splitter
	b.WriteByte('\n')
	b.WriteString(strings.Repeat("─", m.width))

	// Input area
	b.WriteByte('\n')
	for i, line := range inputLines {
		b.WriteString(line)
		if i < len(inputLines)-1 {
			b.WriteByte('\n')
		}
	}

	return b.String()
}

func kittyFilter(m tea.Model, msg tea.Msg) tea.Msg {
	// Method 1: reflection on bubbletea's unexported unknownCSISequenceMsg type.
	v := reflect.ValueOf(msg)
	if v.Kind() == reflect.Slice {
		t := v.Type()
		if t.PkgPath() == "github.com/charmbracelet/bubbletea" && t.Name() == "unknownCSISequenceMsg" {
			return parseKittySequence(v.Bytes())
		}
	}

	// Method 2: fallback via fmt.Stringer for safety.
	if s, ok := msg.(fmt.Stringer); ok {
		str := s.String()
		if strings.HasPrefix(str, "?CSI[") && strings.HasSuffix(str, "?") {
			inner := str[5 : len(str)-2]
			if len(inner) > 0 && inner[0] == '[' {
				inner = inner[1:]
			}
			parts := strings.Fields(inner)
			if len(parts) > 0 {
				seq := make([]byte, len(parts))
				for i, p := range parts {
					b, _ := strconv.Atoi(p)
					seq[i] = byte(b)
				}
				return parseKittySequence(append([]byte{0x1b, '['}, append(seq, 'u')...))
			}
		}
	}

	return msg
}

func parseKittySequence(seq []byte) tea.Msg {
	if len(seq) < 4 || !bytes.HasPrefix(seq, []byte{0x1b, '['}) || seq[len(seq)-1] != 'u' {
		return nil
	}

	inner := string(seq[2 : len(seq)-1])
	fields := strings.Split(inner, ";")
	if len(fields) == 0 {
		return nil
	}

	code, err := strconv.Atoi(fields[0])
	if err != nil {
		return nil
	}

	mod := 1
	event := 1 // 1=press, 2=repeat, 3=release
	if len(fields) > 1 {
		modEvent := strings.Split(fields[1], ":")
		mod, _ = strconv.Atoi(modEvent[0])
		if len(modEvent) > 1 {
			event, _ = strconv.Atoi(modEvent[1])
		}
	}

	// Swallow repeats and releases; only act on key-press.
	if event != 1 && event != 0 {
		return nil
	}

	alt := mod == 3 || mod == 4 || mod == 7 || mod == 8
	shift := mod == 2 || mod == 4 || mod == 6 || mod == 8
	ctrl := mod == 5 || mod == 6 || mod == 7 || mod == 8

	switch code {
	case 13: // Enter
		if shift && !ctrl {
			return shiftEnterMsg{}
		}
		return tea.KeyMsg(tea.Key{Type: tea.KeyEnter, Alt: alt})
	case 9: // Tab
		if shift {
			return tea.KeyMsg(tea.Key{Type: tea.KeyShiftTab, Alt: alt})
		}
		return tea.KeyMsg(tea.Key{Type: tea.KeyTab, Alt: alt})
	case 127: // Backspace
		return tea.KeyMsg(tea.Key{Type: tea.KeyBackspace, Alt: alt})
	case 27: // Escape
		return tea.KeyMsg(tea.Key{Type: tea.KeyEscape, Alt: alt})
	case 32: // Space
		return tea.KeyMsg(tea.Key{Type: tea.KeySpace, Alt: alt})
	}

	// Kitty extended key codes.
	switch code {
	case 57350:
		return tea.KeyMsg(tea.Key{Type: tea.KeyUp, Alt: alt})
	case 57351:
		return tea.KeyMsg(tea.Key{Type: tea.KeyDown, Alt: alt})
	case 57352:
		return tea.KeyMsg(tea.Key{Type: tea.KeyRight, Alt: alt})
	case 57353:
		return tea.KeyMsg(tea.Key{Type: tea.KeyLeft, Alt: alt})
	case 57354:
		return tea.KeyMsg(tea.Key{Type: tea.KeyHome, Alt: alt})
	case 57355:
		return tea.KeyMsg(tea.Key{Type: tea.KeyEnd, Alt: alt})
	case 57356:
		return tea.KeyMsg(tea.Key{Type: tea.KeyPgUp, Alt: alt})
	case 57357:
		return tea.KeyMsg(tea.Key{Type: tea.KeyPgDown, Alt: alt})
	}

	// Printable ASCII.
	if code >= 32 && code <= 126 {
		r := rune(code)
		if shift && code >= 97 && code <= 122 {
			r = rune(code - 32)
		}
		return tea.KeyMsg(tea.Key{Type: tea.KeyRunes, Runes: []rune{r}, Alt: alt})
	}

	return nil
}

func main() {
	m := initialModel()

	python, _ := exec.LookPath("python3")
	if python == "" {
		python, _ = exec.LookPath("python")
	}

	exe, _ := os.Executable()
	workerPath := filepath.Join(filepath.Dir(exe), "..", "main.py")

	p := tea.NewProgram(m, tea.WithMouseAllMotion(), tea.WithFilter(kittyFilter))
	if p == nil {
		fmt.Fprintln(os.Stderr, "failed to create program")
		os.Exit(1)
	}

	w, err := protocol.Spawn(p.Send, python, workerPath)
	if err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
	m.worker = w

	if _, err := p.Run(); err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
}
