package main

import (
	"encoding/json"
	"fmt"
	"os"
	"os/exec"
	"os/signal"
	"path/filepath"
	"runtime"
	"strings"
	"syscall"
	"time"

	"tui/handler/protocol"

	tea "charm.land/bubbletea/v2"
	"github.com/charmbracelet/x/cellbuf"
)

const (
	reverseOn  = "\x1b[7m"
	reverseOff = "\x1b[27m"
)

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
	worker        *protocol.Worker
	content       []string
	wrapped       []string
	inputRunes    []rune
	cursorPos     int // rune position in flat input
	scroll        int // wrapped lines scrolled up from bottom
	width         int
	height        int
	msgID         int
	status        statusData
	lastInputTime time.Time
	idleTimerID   int
	thinkingID    int
	thinkingFrame int
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
	inputH := max(len(m.buildInputView().lines), 1)
	return max(m.height-inputH-1, 1) // -1 splitter
}

func (m *model) maxScroll() int {
	return max(len(m.wrapped)-m.contentHeight(), 0)
}

// wrapLine splits one content line into display rows at the current width.
func (m *model) wrapLine(line string) []string {
	if m.width <= 0 {
		return []string{line}
	}
	return strings.Split(cellbuf.Wrap(line, m.width, ""), "\n")
}

func (m *model) rebuildWrapped() {
	m.wrapped = make([]string, 0, len(m.content)*2)
	for _, line := range m.content {
		m.wrapped = append(m.wrapped, m.wrapLine(line)...)
	}
	m.scroll = min(m.scroll, m.maxScroll())
}

func (m *model) appendContent(line string) {
	m.content = append(m.content, line)
	newWrapped := m.wrapLine(line)
	m.wrapped = append(m.wrapped, newWrapped...)
	// Near the bottom: stick to it. Scrolled up: hold position.
	if m.scroll < 5 {
		m.scroll = 0
	} else {
		m.scroll = min(m.scroll+len(newWrapped), m.maxScroll())
	}
}

func (m *model) scrollBy(n int) {
	m.scroll = min(max(m.scroll+n, 0), m.maxScroll())
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

// moveCursorLine moves the cursor one display line up (dir=-1) or down
// (dir=+1), preserving the visual column where possible.
func (m *model) moveCursorLine(dir int) {
	v := m.buildInputView()
	i, j := v.cursorLine, v.cursorLine+dir
	if i < 0 || j < 0 || j >= len(v.meta) {
		return
	}
	cur, tgt := v.meta[i], v.meta[j]
	col := m.cursorPos - cur.startPos + cur.prefixLen
	col = min(col, tgt.prefixLen+(tgt.endPos-tgt.startPos))
	m.cursorPos = min(tgt.startPos+(col-tgt.prefixLen), tgt.endPos)
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
	avail := max(m.width-len(prompt)-1, 1)

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

			v.lines = append(v.lines, prefix+wl)
			v.meta = append(v.meta, inputLine{
				text:      prefix + wl,
				prefixLen: len([]rune(prefix)),
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

	switch {
	case col < len(line):
		after := ""
		if col+1 < len(line) {
			after = string(line[col+1:])
		}
		lines[v.cursorLine] = string(line[:col]) + reverseOn + string(line[col]) + reverseOff + after
	case len(line) > 0 && len(line) >= m.width:
		// Cursor at end of a full-width line: reverse the last character
		// instead of appending, so we never write m.width+1 cells on one row.
		lines[v.cursorLine] = string(line[:len(line)-1]) + reverseOn + string(line[len(line)-1]) + reverseOff
	default:
		lines[v.cursorLine] = string(line) + reverseOn + " " + reverseOff
	}

	return lines
}

func (m *model) hasMultipleInputLines() bool {
	return len(m.buildInputView().lines) > 1
}

func (m *model) Init() tea.Cmd {
	return nil
}

func (m *model) insertRunes(rs ...rune) {
	m.inputRunes = append(m.inputRunes[:m.cursorPos], append(rs, m.inputRunes[m.cursorPos:]...)...)
	m.cursorPos += len(rs)
}

func (m *model) quit() (tea.Model, tea.Cmd) {
	if m.worker != nil {
		quitData, _ := json.Marshal(protocol.CmdData{Action: "stop"})
		m.worker.SendEnv(protocol.Envelope{Type: "cmd", ID: "quit", Data: quitData})
	}
	return m, tea.Quit
}

func (m *model) handleKey(msg tea.KeyPressMsg) (tea.Model, tea.Cmd) {
	ctrl := msg.Mod&tea.ModCtrl != 0
	alt := msg.Mod&tea.ModAlt != 0
	shift := msg.Mod&tea.ModShift != 0

	// Every key counts as input activity; sendChat/quit below override cmd.
	cmd := m.touchInput()

	switch {
	case msg.Code == 'c' && ctrl:
		return m.quit()

	case msg.Code == tea.KeyEnter:
		if alt || shift {
			m.insertRunes('\n')
		} else if len(m.inputRunes) > 0 {
			cmd = m.sendChat(string(m.inputRunes))
			m.inputRunes = m.inputRunes[:0]
			m.cursorPos = 0
		}

	case msg.Code == tea.KeySpace:
		m.insertRunes(' ')

	case msg.Code == tea.KeyTab:
		m.insertRunes('\t')

	case msg.Code == tea.KeyBackspace:
		if m.cursorPos > 0 {
			m.inputRunes = append(m.inputRunes[:m.cursorPos-1], m.inputRunes[m.cursorPos:]...)
			m.cursorPos--
		}

	case msg.Code == tea.KeyDelete:
		if m.cursorPos < len(m.inputRunes) {
			m.inputRunes = append(m.inputRunes[:m.cursorPos], m.inputRunes[m.cursorPos+1:]...)
		}

	case msg.Code == 'u' && ctrl:
		m.inputRunes = m.inputRunes[m.cursorPos:]
		m.cursorPos = 0

	case msg.Code == 'w' && ctrl:
		m.cursorPos = m.deleteWordBackward()
		m.inputRunes = m.inputRunes[m.cursorPos:]

	case msg.Code == tea.KeyLeft:
		m.cursorPos = max(m.cursorPos-1, 0)

	case msg.Code == tea.KeyRight:
		m.cursorPos = min(m.cursorPos+1, len(m.inputRunes))

	case msg.Code == tea.KeyUp:
		if m.hasMultipleInputLines() {
			m.moveCursorLine(-1)
		} else {
			m.scrollBy(1)
		}

	case msg.Code == tea.KeyDown:
		if m.hasMultipleInputLines() {
			m.moveCursorLine(1)
		} else {
			m.scrollBy(-1)
		}

	case msg.Code == tea.KeyPgUp:
		m.scrollBy(m.contentHeight())

	case msg.Code == tea.KeyPgDown:
		m.scrollBy(-m.contentHeight())

	case msg.Code == tea.KeyHome:
		m.cursorPos = 0

	case msg.Code == tea.KeyEnd:
		m.cursorPos = len(m.inputRunes)

	default:
		if msg.Text != "" && !ctrl && !alt {
			m.insertRunes([]rune(msg.Text)...)
		}
	}
	return m, cmd
}

// handleEvent processes one JSON line from the Python worker.
func (m *model) handleEvent(raw []byte) tea.Cmd {
	var env protocol.Envelope
	if err := json.Unmarshal(raw, &env); err != nil {
		m.appendContent(fmt.Sprintf("[bad json] %s", raw))
		return nil
	}

	if env.Type == "status" && (string(env.Data) == `{"state":"ready"}` || string(env.Data) == `{"state":"startup"}`) && m.worker != nil {
		termData, _ := json.Marshal(getTerminalInfo())
		m.worker.SendEnv(protocol.Envelope{Type: "terminal", ID: env.ID, Data: termData})
	}

	var cmd tea.Cmd
	var payload map[string]any
	_ = json.Unmarshal(env.Data, &payload)

	oldState := m.status.State
	if s, ok := payload["status"].(string); ok && s != "" {
		m.status.State = s
	} else if s, ok := payload["state"].(string); ok && s != "" {
		switch s {
		case "error":
			m.status.State = "error"
		case "done":
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
	return cmd
}

func (m *model) Update(msg tea.Msg) (tea.Model, tea.Cmd) {
	switch msg := msg.(type) {
	case idleTimerMsg:
		if msg.ID == m.idleTimerID && m.status.State == "done" && time.Since(m.lastInputTime) >= 59*time.Second {
			m.status.State = "idle"
		}

	case thinkingTickMsg:
		if msg.ID == m.thinkingID {
			m.thinkingFrame++
			return m, startThinking(m.thinkingID)
		}

	case tea.WindowSizeMsg:
		m.width = msg.Width
		m.height = msg.Height
		m.rebuildWrapped()

	case tea.InterruptMsg:
		return m.quit()

	case tea.MouseWheelMsg:
		switch msg.Button {
		case tea.MouseWheelUp:
			m.scrollBy(1)
		case tea.MouseWheelDown:
			m.scrollBy(-1)
		}
		return m, m.touchInput()

	case tea.KeyPressMsg:
		return m.handleKey(msg)

	case protocol.EventMsg:
		return m, m.handleEvent(msg.Raw)
	}
	return m, nil
}

func (m *model) View() tea.View {
	v := tea.NewView(m.render())
	v.AltScreen = true
	v.MouseMode = tea.MouseModeAllMotion
	v.KeyboardEnhancements.ReportEventTypes = true
	return v
}

// writePadded writes s padded with spaces to the full terminal width.
func (m *model) writePadded(b *strings.Builder, s string) {
	b.WriteString(Bg)
	b.WriteString(s)
	if pad := m.width - visualWidth(s); pad > 0 {
		b.WriteString(strings.Repeat(" ", pad))
	}
}

func (m *model) render() string {
	if m.width == 0 || m.height == 0 {
		return "loading..."
	}
	if m.height < 3 {
		return "terminal too small\n"
	}

	var b strings.Builder

	inputLines := m.renderInputWithCursor(m.buildInputView())
	contentH := max(m.height-max(len(inputLines), 1)-2, 1) // blank line + statusline

	total := len(m.wrapped)
	start := max(total-contentH-m.scroll, 0)

	for i := range contentH {
		if idx := start + i; idx < total {
			m.writePadded(&b, m.wrapped[idx])
		} else {
			m.writePadded(&b, "")
		}
		if i < contentH-1 {
			b.WriteByte('\n')
		}
	}

	// Blank separator + statusline
	b.WriteString("\n\n")
	m.writePadded(&b, renderStatusline(m))

	// Input area
	b.WriteByte('\n')
	for i, line := range inputLines {
		m.writePadded(&b, line)
		if i < len(inputLines)-1 {
			b.WriteByte('\n')
		}
	}

	b.WriteString(Reset)
	return b.String()
}

func main() {
	if len(os.Args) > 1 && os.Args[1] == "-demo" {
		runDemo()
		return
	}

	m := initialModel()

	python, _ := exec.LookPath("python3")
	if python == "" {
		python, _ = exec.LookPath("python")
	}

	exe, _ := os.Executable()
	workerPath := filepath.Join(filepath.Dir(exe), "..", "main.py")

	p := tea.NewProgram(m)

	w, err := protocol.Spawn(func(msg any) { p.Send(msg) }, python, workerPath)
	if err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
	m.worker = w
	defer w.Kill()

	go func() {
		c := make(chan os.Signal, 1)
		signal.Notify(c, syscall.SIGTERM, syscall.SIGHUP)
		<-c
		w.Kill()
		os.Exit(1)
	}()

	if _, err := p.Run(); err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
}
