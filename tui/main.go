package main

import (
	"encoding/json"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"strings"

	"tui/handler"

	tea "github.com/charmbracelet/bubbletea"
)

type model struct {
	worker *handler.Worker
	events []string
}

func (m *model) Init() tea.Cmd { return nil }

func (m *model) Update(msg tea.Msg) (tea.Model, tea.Cmd) {
	switch msg := msg.(type) {
	case tea.KeyMsg:
		switch msg.String() {
		case "q", "ctrl+c":
			return m, tea.Quit
		case "p":
			if m.worker != nil {
				m.worker.Pause()
				m.events = append(m.events, "> pause")
			}
		case "s":
			if m.worker != nil {
				m.worker.Stop()
				m.events = append(m.events, "> stop")
			}
		case "i":
			if m.worker != nil {
				m.worker.Inject("hello")
				m.events = append(m.events, "> inject hello")
			}
		}
	case handler.EventMsg:
		switch msg.Type {
		case "status":
			var d handler.StatusData
			json.Unmarshal(msg.Data, &d)
			m.events = append(m.events, fmt.Sprintf("status: %s", d.State))
			if d.Text != "" {
				m.events = append(m.events, fmt.Sprintf("  text: %s", d.Text))
			}
		default:
			m.events = append(m.events, fmt.Sprintf("[%s] %s", msg.Type, string(msg.Data)))
		}
	}
	return m, nil
}

func (m *model) View() string {
	var b strings.Builder
	b.WriteString("TUI Protocol Demo — keys: p=pause s=stop i=inject q=quit\n\n")
	for _, e := range m.events {
		b.WriteString(e)
		b.WriteByte('\n')
	}
	return b.String()
}

func findPython() (string, error) {
	for _, name := range []string{"python3", "python", "py"} {
		if p, err := exec.LookPath(name); err == nil {
			return p, nil
		}
	}
	return "", fmt.Errorf("no python interpreter found (tried: python3, python, py)")
}

func main() {
	m := &model{}
	p := tea.NewProgram(m)

	python, err := findPython()
	if err != nil {
		fmt.Fprintf(os.Stderr, "error: %v\n", err)
		os.Exit(1)
	}

	exe, _ := os.Executable()
	base := filepath.Dir(exe)
	worker := filepath.Join(base, "..", "main.py")

	if _, err := os.Stat(worker); os.IsNotExist(err) {
		fmt.Fprintf(os.Stderr, "error: worker script not found: %s\n", worker)
		os.Exit(1)
	}

	w, err := handler.Spawn(p.Send, python, worker)
	if err != nil {
		fmt.Fprintf(os.Stderr, "spawn: %v\n", err)
		os.Exit(1)
	}
	m.worker = w

	if _, err := p.Run(); err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
}
