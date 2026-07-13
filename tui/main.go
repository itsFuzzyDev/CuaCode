package main

import (
	"bytes"
	"encoding/json"
	"fmt"
	"os"
	"os/exec"
	"os/signal"
	"path/filepath"
	"reflect"
	"runtime"
	"strconv"
	"strings"
	"syscall"
	"time"

	"tui/handler/protocol"

	tea "github.com/charmbracelet/bubbletea"
	"github.com/charmbracelet/x/cellbuf"
)

const (
	reverseOn  = "\x1b[7m"
	reverseOff = "\x1b[0m"
)

type shiftEnterMsg struct{}

type statusData struct {
	State       string // idle | running | tools | done | error
	Msgs        int    // total messages in conversation
	Turns       int    // assistant response turns completed
	LastToken   string // last streaming token (truncated)
	Error       string // last error text
	ContextLeft int    // tokens remaining (populated by worker if reported)
}

type idleTimerMsg struct {
	ID int
}

type thinkingTickMsg struct {
	ID int
}

type model struct {
	worker         *protocol.Worker
	content        []string
	wrapped        []string
	inputRunes     []rune
	cursorPos      int       // rune position in flat input
	scroll         int       // wrapped lines scrolled up from bottom
	width          int
	height         int
	msgID          int
	status         statusData
	lastInputTime  time.Time
	idleTimerID    int
	thinkingID     int
	thinkingFrame  int
}

func initialModel() *model {
	return &model{
		content:       make([]string, 0),
		wrapped:       make([]string, 0),
		status:        statusData{State: "idle"},
		lastInputTime: time.Now(),
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

func startIdleTimer(id int) tea.Cmd {
	return tea.Tick(time.Minute, func(t time.Time) tea.Msg {
		return idleTimerMsg{ID: id}
	})
}

func startThinking(id int) tea.Cmd {
	return tea.Tick(120*time.Millisecond, func(t time.Time) tea.Msg {
		return thinkingTickMsg{ID: id}
	})
}

func (m *model) touchInput() tea.Cmd {
	m.lastInputTime = time.Now()
	if m.status.State == "done" {
		m.idleTimerID++
		return startIdleTimer(m.idleTimerID)
	}
	return nil
}

func (m *model) sendChat(text string) tea.Cmd {
	m.msgID++
	m.status.Msgs++
	m.status.State = "running"
	m.lastInputTime = time.Now()
	m.idleTimerID++
	m.thinkingID++
	m.thinkingFrame = 0
	data, _ := json.Marshal(protocol.CmdData{Action: "chat", Text: text})
	env := protocol.Envelope{
		Type: "cmd",
		ID:   fmt.Sprintf("msg-%d", m.msgID),
		Data: data,
	}
	m.worker.SendEnv(env)
	m.appendContent("> " + text)
	return startThinking(m.thinkingID)
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
	v := m.buildInputView()
	if v.cursorLine <= 0 {
		return
	}
	cur := v.meta[v.cursorLine]
	prev := v.meta[v.cursorLine-1]

	// Preserve visual column (including prefix)
	curCol := m.cursorPos - cur.startPos + cur.prefixLen
	maxCol := prev.prefixLen + (prev.endPos - prev.startPos)
	targetCol := curCol
	if targetCol > maxCol {
		targetCol = maxCol
	}

	m.cursorPos = prev.startPos + (targetCol - prev.prefixLen)
	if m.cursorPos > prev.endPos {
		m.cursorPos = prev.endPos
	}
}

func (m *model) moveCursorLineDown() {
	v := m.buildInputView()
	if v.cursorLine < 0 || v.cursorLine >= len(v.lines)-1 {
		return
	}
	cur := v.meta[v.cursorLine]
	next := v.meta[v.cursorLine+1]

	curCol := m.cursorPos - cur.startPos + cur.prefixLen
	maxCol := next.prefixLen + (next.endPos - next.startPos)
	targetCol := curCol
	if targetCol > maxCol {
		targetCol = maxCol
	}

	m.cursorPos = next.startPos + (targetCol - next.prefixLen)
	if m.cursorPos > next.endPos {
		m.cursorPos = next.endPos
	}
}

type inputLine struct {
	text      string
	prefixLen int
	startPos  int // inclusive in inputRunes
	endPos    int // exclusive in inputRunes
}

type inputView struct {
	lines      []string
	cursorLine int
	cursorCol  int
	meta       []inputLine
}

func (m *model) buildInputView() inputView {
	prompt := "> "
	indent := strings.Repeat(" ", len(prompt))
	avail := m.width - len(prompt) - 1
	if avail < 1 {
		avail = 1
	}

	v := inputView{cursorLine: -1, cursorCol: 0}
	cursorPos := m.cursorPos
	logicalLines := strings.Split(string(m.inputRunes), "\n")

	runeOffset := 0
	for li, line := range logicalLines {
		var wrappedLines []string
		if len(line) == 0 {
			wrappedLines = []string{""}
		} else {
			w := cellbuf.Wrap(line, avail, "")
			wrappedLines = strings.Split(w, "\n")
		}

		for wi, wl := range wrappedLines {
			prefix := indent
			if li == 0 && wi == 0 {
				prefix = prompt
			}

			wlRunes := []rune(wl)
			startPos := runeOffset
			endPos := runeOffset + len(wlRunes)
			prefixLen := len([]rune(prefix))

			v.lines = append(v.lines, prefix+wl)
			v.meta = append(v.meta, inputLine{
				text:      prefix + wl,
				prefixLen: prefixLen,
				startPos:  startPos,
				endPos:    endPos,
			})
			runeOffset = endPos
		}

		if li < len(logicalLines)-1 {
			runeOffset++ // \n
		}
	}

	if len(v.lines) == 0 {
		v.lines = []string{prompt}
		v.meta = append(v.meta, inputLine{text: prompt, prefixLen: len(prompt), startPos: 0, endPos: 0})
		v.cursorLine = 0
		v.cursorCol = len(prompt)
		return v
	}

	// Resolve cursor position.  Scan in reverse so that when cursorPos sits
	// exactly on a segment boundary (cursorPos == endPos == next.startPos) we
	// prefer the segment that ENDS there rather than the one that starts there.
	for i := len(v.meta) - 1; i >= 0; i-- {
		seg := v.meta[i]
		if cursorPos >= seg.startPos && cursorPos <= seg.endPos {
			// For non-last segments, treat the boundary (cursorPos == endPos)
			// as belonging to this segment.
			if cursorPos == seg.endPos && i < len(v.meta)-1 {
				// Still prefer this segment unless it is empty and the next is non-empty
				next := v.meta[i+1]
				if seg.endPos > seg.startPos || next.endPos == next.startPos {
					v.cursorLine = i
					v.cursorCol = len([]rune(seg.text))
					break
				}
				// Empty segment followed by non-empty: fall through to next segment
				continue
			}
			v.cursorLine = i
			v.cursorCol = seg.prefixLen + (cursorPos - seg.startPos)
			break
		}
	}

	// Very end of input (past the last segment)
	if v.cursorLine < 0 && cursorPos >= runeOffset {
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

	if col < len(line) {
		before := string(line[:col])
		at := string(line[col])
		after := ""
		if col+1 < len(line) {
			after = string(line[col+1:])
		}
		lines[v.cursorLine] = before + reverseOn + at + reverseOff + after
	} else {
		// Cursor at end of line.  If adding a space would overflow the terminal
		// width, reverse the last character instead so we never write m.width
		// characters on one row.
		if len(line) > 0 && len(line) >= m.width {
			lines[v.cursorLine] = string(line[:len(line)-1]) + reverseOn + string(line[len(line)-1]) + reverseOff
		} else {
			lines[v.cursorLine] = string(line) + reverseOn + " " + reverseOff
		}
	}

	return lines
}

func (m *model) hasMultipleInputLines() bool {
	v := m.buildInputView()
	return len(v.lines) > 1
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
	var cmd tea.Cmd

	switch msg := msg.(type) {
	case idleTimerMsg:
		if msg.ID == m.idleTimerID && m.status.State == "done" && time.Since(m.lastInputTime) >= 59*time.Second {
			m.status.State = "idle"
		}
		return m, nil

	case thinkingTickMsg:
		if msg.ID == m.thinkingID {
			m.thinkingFrame++
			return m, startThinking(m.thinkingID)
		}
		return m, nil

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
		cmd = m.touchInput()

	case shiftEnterMsg:
		m.inputRunes = append(m.inputRunes[:m.cursorPos], append([]rune{'\n'}, m.inputRunes[m.cursorPos:]...)...)
		m.cursorPos++
		cmd = m.touchInput()

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
				cmd = m.touchInput()
			} else if len(m.inputRunes) > 0 {
				cmd = m.sendChat(string(m.inputRunes))
				m.inputRunes = m.inputRunes[:0]
				m.cursorPos = 0
			}
		case tea.KeySpace:
			m.inputRunes = append(m.inputRunes[:m.cursorPos], append([]rune{' '}, m.inputRunes[m.cursorPos:]...)...)
			m.cursorPos++
			cmd = m.touchInput()
		case tea.KeyTab:
			m.inputRunes = append(m.inputRunes[:m.cursorPos], append([]rune{'\t'}, m.inputRunes[m.cursorPos:]...)...)
			m.cursorPos++
			cmd = m.touchInput()
		case tea.KeyShiftTab:
			m.inputRunes = append(m.inputRunes[:m.cursorPos], append([]rune{'\t'}, m.inputRunes[m.cursorPos:]...)...)
			m.cursorPos++
			cmd = m.touchInput()
		case tea.KeyBackspace:
			if m.cursorPos > 0 {
				m.inputRunes = append(m.inputRunes[:m.cursorPos-1], m.inputRunes[m.cursorPos:]...)
				m.cursorPos--
			}
			cmd = m.touchInput()
		case tea.KeyDelete:
			if m.cursorPos < len(m.inputRunes) {
				m.inputRunes = append(m.inputRunes[:m.cursorPos], m.inputRunes[m.cursorPos+1:]...)
			}
			cmd = m.touchInput()
		case tea.KeyCtrlU:
			m.inputRunes = m.inputRunes[m.cursorPos:]
			m.cursorPos = 0
			cmd = m.touchInput()
		case tea.KeyCtrlW:
			m.cursorPos = m.deleteWordBackward()
			m.inputRunes = m.inputRunes[m.cursorPos:]
			cmd = m.touchInput()
		case tea.KeyLeft:
			if m.cursorPos > 0 {
				m.cursorPos--
			}
			cmd = m.touchInput()
		case tea.KeyRight:
			if m.cursorPos < len(m.inputRunes) {
				m.cursorPos++
			}
			cmd = m.touchInput()
		case tea.KeyUp:
			if m.hasMultipleInputLines() {
				m.moveCursorLineUp()
			} else {
				m.scrollUp(1)
			}
			cmd = m.touchInput()
		case tea.KeyDown:
			if m.hasMultipleInputLines() {
				m.moveCursorLineDown()
			} else {
				m.scrollDown(1)
			}
			cmd = m.touchInput()
		case tea.KeyPgUp:
			m.scrollUp(m.contentHeight())
			cmd = m.touchInput()
		case tea.KeyPgDown:
			m.scrollDown(m.contentHeight())
			cmd = m.touchInput()
		case tea.KeyHome:
			m.cursorPos = 0
			cmd = m.touchInput()
		case tea.KeyEnd:
			m.cursorPos = len(m.inputRunes)
			cmd = m.touchInput()
		case tea.KeyRunes:
			for _, r := range msg.Runes {
				m.inputRunes = append(m.inputRunes[:m.cursorPos], append([]rune{r}, m.inputRunes[m.cursorPos:]...)...)
				m.cursorPos++
			}
			cmd = m.touchInput()
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

			// Parse structured status from token/status events.
			var payload map[string]interface{}
			_ = json.Unmarshal(env.Data, &payload)
			oldState := m.status.State
			if s, ok := payload["status"].(string); ok && s != "" {
				m.status.State = s
			} else if s, ok := payload["state"].(string); ok && s != "" {
				if s == "error" {
					m.status.State = "error"
				} else if s == "done" {
					m.status.State = "done"
					m.status.Turns++
					m.idleTimerID++
					cmd = startIdleTimer(m.idleTimerID)
				}
			}
			// Start/stop thinking animation based on state transitions.
			if oldState != m.status.State {
				switch m.status.State {
				case "running", "tools", "tooling":
					m.thinkingID++
					m.thinkingFrame = 0
					if cmd != nil {
						cmd = tea.Batch(cmd, startThinking(m.thinkingID))
					} else {
						cmd = startThinking(m.thinkingID)
					}
				default:
					m.thinkingID++ // invalidate pending ticks
				}
			}
			if s, ok := payload["token"].(string); ok {
				m.status.LastToken = s
			}
			if s, ok := payload["error"].(string); ok {
				m.status.Error = s
			}
			if n, ok := payload["msg_count"].(float64); ok {
				m.status.Msgs = int(n)
			}
			if n, ok := payload["context_left"].(float64); ok {
				m.status.ContextLeft = int(n)
			}

			m.appendContent(fmt.Sprintf("[%s|%s] %s", env.Type, env.ID, string(env.Data)))
		}
	}
	return m, cmd
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

		contentH := m.height - inputH - 2 // blank line + statusline
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

		// Blank separator + statusline
		b.WriteByte('\n')
		b.WriteByte('\n')
		b.WriteString(renderStatusline(m))

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
	v := reflect.ValueOf(msg)
	if v.Kind() == reflect.Slice {
		t := v.Type()
		if t.PkgPath() == "github.com/charmbracelet/bubbletea" && t.Name() == "unknownCSISequenceMsg" {
			if kmsg, ok := parseKittySequence(v.Bytes()); ok {
				return kmsg
			}
		}
	}
	return msg
}

// parseKittySequence interprets CSI `u` sequences from the kitty keyboard protocol.
// It returns (tea.Msg, true) when it recognizes a key, otherwise (_, false) so the
// caller falls back to the original unhandled message.
func parseKittySequence(seq []byte) (tea.Msg, bool) {
	if len(seq) < 4 || !bytes.HasPrefix(seq, []byte{0x1b, '['}) || seq[len(seq)-1] != 'u' {
		return nil, false
	}

	inner := string(seq[2 : len(seq)-1])
	fields := strings.Split(inner, ";")
	if len(fields) == 0 {
		return nil, false
	}

	code, err := strconv.Atoi(fields[0])
	if err != nil {
		return nil, false
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
		return nil, true
	}

	alt := mod == 3 || mod == 4 || mod == 7 || mod == 8
	shift := mod == 2 || mod == 4 || mod == 6 || mod == 8
	ctrl := mod == 5 || mod == 6 || mod == 7 || mod == 8

	// Ctrl+C, Ctrl+A-Z, etc. Kitty sends the base Unicode codepoint with ctrl modifier.
	if ctrl && !alt && code >= 97 && code <= 122 {
		switch code {
		case 97:
			return tea.KeyMsg(tea.Key{Type: tea.KeyCtrlA}), true
		case 98:
			return tea.KeyMsg(tea.Key{Type: tea.KeyCtrlB}), true
		case 99:
			return tea.KeyMsg(tea.Key{Type: tea.KeyCtrlC}), true
		case 100:
			return tea.KeyMsg(tea.Key{Type: tea.KeyCtrlD}), true
		case 101:
			return tea.KeyMsg(tea.Key{Type: tea.KeyCtrlE}), true
		case 102:
			return tea.KeyMsg(tea.Key{Type: tea.KeyCtrlF}), true
		case 103:
			return tea.KeyMsg(tea.Key{Type: tea.KeyCtrlG}), true
		case 104:
			return tea.KeyMsg(tea.Key{Type: tea.KeyCtrlH}), true
		case 105:
			return tea.KeyMsg(tea.Key{Type: tea.KeyCtrlI}), true
		case 106:
			return tea.KeyMsg(tea.Key{Type: tea.KeyCtrlJ}), true
		case 107:
			return tea.KeyMsg(tea.Key{Type: tea.KeyCtrlK}), true
		case 108:
			return tea.KeyMsg(tea.Key{Type: tea.KeyCtrlL}), true
		case 109:
			return tea.KeyMsg(tea.Key{Type: tea.KeyCtrlM}), true
		case 110:
			return tea.KeyMsg(tea.Key{Type: tea.KeyCtrlN}), true
		case 111:
			return tea.KeyMsg(tea.Key{Type: tea.KeyCtrlO}), true
		case 112:
			return tea.KeyMsg(tea.Key{Type: tea.KeyCtrlP}), true
		case 113:
			return tea.KeyMsg(tea.Key{Type: tea.KeyCtrlQ}), true
		case 114:
			return tea.KeyMsg(tea.Key{Type: tea.KeyCtrlR}), true
		case 115:
			return tea.KeyMsg(tea.Key{Type: tea.KeyCtrlS}), true
		case 116:
			return tea.KeyMsg(tea.Key{Type: tea.KeyCtrlT}), true
		case 117:
			return tea.KeyMsg(tea.Key{Type: tea.KeyCtrlU}), true
		case 118:
			return tea.KeyMsg(tea.Key{Type: tea.KeyCtrlV}), true
		case 119:
			return tea.KeyMsg(tea.Key{Type: tea.KeyCtrlW}), true
		case 120:
			return tea.KeyMsg(tea.Key{Type: tea.KeyCtrlX}), true
		case 121:
			return tea.KeyMsg(tea.Key{Type: tea.KeyCtrlY}), true
		case 122:
			return tea.KeyMsg(tea.Key{Type: tea.KeyCtrlZ}), true
		}
	}

	// Ctrl+special symbols
	if ctrl && !alt {
		switch code {
		case 64:
			return tea.KeyMsg(tea.Key{Type: tea.KeyCtrlAt}), true
		case 91:
			return tea.KeyMsg(tea.Key{Type: tea.KeyCtrlOpenBracket}), true
		case 92:
			return tea.KeyMsg(tea.Key{Type: tea.KeyCtrlBackslash}), true
		case 93:
			return tea.KeyMsg(tea.Key{Type: tea.KeyCtrlCloseBracket}), true
		case 94:
			return tea.KeyMsg(tea.Key{Type: tea.KeyCtrlCaret}), true
		case 95:
			return tea.KeyMsg(tea.Key{Type: tea.KeyCtrlUnderscore}), true
		case 63:
			return tea.KeyMsg(tea.Key{Type: tea.KeyCtrlQuestionMark}), true
		}
	}

	switch code {
	case 13: // Enter
		if shift && !ctrl {
			return shiftEnterMsg{}, true
		}
		return tea.KeyMsg(tea.Key{Type: tea.KeyEnter, Alt: alt}), true
	case 9: // Tab
		if shift {
			return tea.KeyMsg(tea.Key{Type: tea.KeyShiftTab, Alt: alt}), true
		}
		return tea.KeyMsg(tea.Key{Type: tea.KeyTab, Alt: alt}), true
	case 127: // Backspace
		return tea.KeyMsg(tea.Key{Type: tea.KeyBackspace, Alt: alt}), true
	case 27: // Escape
		return tea.KeyMsg(tea.Key{Type: tea.KeyEscape, Alt: alt}), true
	case 32: // Space
		return tea.KeyMsg(tea.Key{Type: tea.KeySpace, Alt: alt}), true
	}

	// Kitty extended key codes.
	switch code {
	case 57350:
		return tea.KeyMsg(tea.Key{Type: tea.KeyUp, Alt: alt}), true
	case 57351:
		return tea.KeyMsg(tea.Key{Type: tea.KeyDown, Alt: alt}), true
	case 57352:
		return tea.KeyMsg(tea.Key{Type: tea.KeyRight, Alt: alt}), true
	case 57353:
		return tea.KeyMsg(tea.Key{Type: tea.KeyLeft, Alt: alt}), true
	case 57354:
		return tea.KeyMsg(tea.Key{Type: tea.KeyHome, Alt: alt}), true
	case 57355:
		return tea.KeyMsg(tea.Key{Type: tea.KeyEnd, Alt: alt}), true
	case 57356:
		return tea.KeyMsg(tea.Key{Type: tea.KeyPgUp, Alt: alt}), true
	case 57357:
		return tea.KeyMsg(tea.Key{Type: tea.KeyPgDown, Alt: alt}), true
	}

	// Printable ASCII (unmodified or shift-only).
	if code >= 32 && code <= 126 {
		r := rune(code)
		if shift && code >= 97 && code <= 122 {
			r = rune(code - 32)
		}
		return tea.KeyMsg(tea.Key{Type: tea.KeyRunes, Runes: []rune{r}, Alt: alt}), true
	}

	return nil, false
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
	defer w.Kill()

	go func() {
		c := make(chan os.Signal, 1)
		signal.Notify(c, os.Interrupt, syscall.SIGTERM, syscall.SIGHUP)
		<-c
		w.Kill()
		os.Exit(1)
	}()

	if _, err := p.Run(); err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
}
