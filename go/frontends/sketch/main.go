// Command sketch is a scaffold for a new terminal frontend: the plumbing is
// finished, the design is not. It spawns the worker, tracks the session,
// takes input, and scrolls — nothing more.
//
// This file is the wiring and should mostly stay as-is. Everything the user
// actually looks at is built in view.go; start there.
package main

import (
	"fmt"
	"os"
	"strings"
	"time"

	"cuacode/core/runner"
	"cuacode/core/session"

	tea "charm.land/bubbletea/v2"
	"github.com/charmbracelet/x/cellbuf"
)

// spinTickMsg drives the busy animation. ID invalidates ticks left over from
// a previous busy period.
type spinTickMsg struct{ ID int }

type model struct {
	sess   *session.Session
	status session.Snapshot // refreshed on every session event

	lines   []string // feed lines as produced, pre-wrap
	wrapped []string // the same lines wrapped to the current width
	scroll  int      // wrapped rows scrolled up from the bottom

	input  []rune
	cursor int // rune index into input

	width, height int

	spinID    int
	spinFrame int
}

func initialModel() *model {
	return &model{status: session.Snapshot{State: session.Idle}}
}

// contentHeight is the number of rows left for the feed once the chrome
// (status row + input row) is accounted for.
func (m *model) contentHeight() int {
	return max(m.height-2, 1)
}

func (m *model) maxScroll() int {
	return max(len(m.wrapped)-m.contentHeight(), 0)
}

func (m *model) wrapLine(line string) []string {
	if m.width <= 0 {
		return []string{line}
	}
	return strings.Split(cellbuf.Wrap(line, m.width, ""), "\n")
}

func (m *model) rebuildWrapped() {
	m.wrapped = make([]string, 0, len(m.lines))
	for _, line := range m.lines {
		m.wrapped = append(m.wrapped, m.wrapLine(line)...)
	}
	m.scroll = min(m.scroll, m.maxScroll())
}

// appendLines adds feed lines, sticking to the bottom unless the user has
// scrolled away from it.
func (m *model) appendLines(lines ...string) {
	for _, line := range lines {
		m.lines = append(m.lines, line)
		w := m.wrapLine(line)
		m.wrapped = append(m.wrapped, w...)
		if m.scroll < 5 {
			m.scroll = 0
		} else {
			m.scroll = min(m.scroll+len(w), m.maxScroll())
		}
	}
}

func (m *model) scrollBy(n int) {
	m.scroll = min(max(m.scroll+n, 0), m.maxScroll())
}

func startSpin(id int) tea.Cmd {
	return tea.Tick(120*time.Millisecond, func(time.Time) tea.Msg {
		return spinTickMsg{ID: id}
	})
}

func (m *model) quit() (tea.Model, tea.Cmd) {
	if m.sess != nil {
		m.sess.Close()
	}
	return m, tea.Quit
}

func (m *model) send(text string) tea.Cmd {
	m.sess.SendChat(text)
	m.status = m.sess.Snapshot()
	m.appendLines(userLine(text))
	m.spinID++
	m.spinFrame = 0
	return startSpin(m.spinID)
}

func (m *model) Init() tea.Cmd { return nil }

func (m *model) handleKey(msg tea.KeyPressMsg) (tea.Model, tea.Cmd) {
	ctrl := msg.Mod&tea.ModCtrl != 0
	alt := msg.Mod&tea.ModAlt != 0

	switch {
	case msg.Code == 'c' && ctrl:
		return m.quit()

	case msg.Code == tea.KeyEsc:
		if m.sess != nil && session.Busy(m.status.State) {
			m.sess.Cancel()
		}

	case msg.Code == tea.KeyEnter:
		if len(m.input) > 0 {
			cmd := m.send(string(m.input))
			m.input, m.cursor = m.input[:0], 0
			return m, cmd
		}

	case msg.Code == tea.KeySpace:
		m.insert(' ')

	case msg.Code == tea.KeyBackspace:
		if m.cursor > 0 {
			m.input = append(m.input[:m.cursor-1], m.input[m.cursor:]...)
			m.cursor--
		}

	case msg.Code == tea.KeyDelete:
		if m.cursor < len(m.input) {
			m.input = append(m.input[:m.cursor], m.input[m.cursor+1:]...)
		}

	case msg.Code == 'u' && ctrl:
		m.input, m.cursor = m.input[m.cursor:], 0

	case msg.Code == 'w' && ctrl:
		start := m.wordStart()
		m.input = append(m.input[:start], m.input[m.cursor:]...)
		m.cursor = start

	case msg.Code == tea.KeyLeft:
		m.cursor = max(m.cursor-1, 0)

	case msg.Code == tea.KeyRight:
		m.cursor = min(m.cursor+1, len(m.input))

	case msg.Code == tea.KeyHome:
		m.cursor = 0

	case msg.Code == tea.KeyEnd:
		m.cursor = len(m.input)

	case msg.Code == tea.KeyUp:
		m.scrollBy(1)

	case msg.Code == tea.KeyDown:
		m.scrollBy(-1)

	case msg.Code == tea.KeyPgUp:
		m.scrollBy(m.contentHeight())

	case msg.Code == tea.KeyPgDown:
		m.scrollBy(-m.contentHeight())

	default:
		if msg.Text != "" && !ctrl && !alt {
			m.insert([]rune(msg.Text)...)
		}
	}
	return m, nil
}

func (m *model) insert(rs ...rune) {
	m.input = append(m.input[:m.cursor], append(rs, m.input[m.cursor:]...)...)
	m.cursor += len(rs)
}

// wordStart is the index one Ctrl+W deletion would leave the cursor at.
func (m *model) wordStart() int {
	i := m.cursor - 1
	for i >= 0 && m.input[i] == ' ' {
		i--
	}
	for i >= 0 && m.input[i] != ' ' {
		i--
	}
	return i + 1
}

// handleSessionEvent folds one worker event into the model. Parsing and state
// bookkeeping already happened in core/session; this only decides what to show
// and whether the spinner runs.
func (m *model) handleSessionEvent(ev session.Event) tea.Cmd {
	if ev.ParseErr == nil {
		m.status = ev.Snapshot
	}
	m.appendLines(feedLines(ev)...)

	if !ev.StateChanged {
		return nil
	}
	m.spinID++ // invalidates any tick still in flight
	if session.Busy(m.status.State) {
		m.spinFrame = 0
		return startSpin(m.spinID)
	}
	return nil
}

func (m *model) Update(msg tea.Msg) (tea.Model, tea.Cmd) {
	switch msg := msg.(type) {
	case spinTickMsg:
		if msg.ID == m.spinID {
			m.spinFrame++
			return m, startSpin(m.spinID)
		}

	case tea.WindowSizeMsg:
		m.width, m.height = msg.Width, msg.Height
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

	case tea.KeyPressMsg:
		return m.handleKey(msg)

	case session.Event:
		return m, m.handleSessionEvent(msg)
	}
	return m, nil
}

func (m *model) View() tea.View {
	v := tea.NewView(m.render())
	v.AltScreen = true
	v.MouseMode = tea.MouseModeAllMotion
	return v
}

func main() {
	m := initialModel()
	p := tea.NewProgram(m)

	sess, err := runner.Start(func(ev session.Event) { p.Send(ev) })
	if err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
	m.sess = sess
	defer sess.Close()

	if _, err := p.Run(); err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
}
