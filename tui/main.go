package main

import (
	"encoding/json"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"strings"

	"tui/handler/protocol"

	tea "github.com/charmbracelet/bubbletea"
)

type model struct {
	worker *protocol.Worker
	lines  []string
}

func (m *model) Init() tea.Cmd { return nil }

func cmd(action, text string) protocol.Envelope {
	data, _ := json.Marshal(protocol.CmdData{Action: action, Text: text})
	return protocol.Envelope{Type: "cmd", ID: action + "1", Data: data}
}

func (m *model) Update(msg tea.Msg) (tea.Model, tea.Cmd) {
	switch msg := msg.(type) {
	case tea.KeyMsg:
		switch msg.String() {
		case "q", "ctrl+c":
			return m, tea.Quit
		case "p":
			m.worker.SendEnv(cmd("pause", ""))
			m.lines = append(m.lines, "> pause")
		case "s":
			m.worker.SendEnv(cmd("stop", ""))
			m.lines = append(m.lines, "> stop")
		case "i":
			m.worker.SendEnv(cmd("inject", "hello"))
			m.lines = append(m.lines, "> inject hello")
		}
	case protocol.EventMsg:
		var env protocol.Envelope
		if err := json.Unmarshal(msg.Raw, &env); err != nil {
			m.lines = append(m.lines, fmt.Sprintf("bad json: %s", msg.Raw))
		} else {
			m.lines = append(m.lines, fmt.Sprintf("[%s|%s] %s", env.Type, env.ID, string(env.Data)))
		}
	}
	return m, nil
}

func (m *model) View() string {
	return "keys: p=pause s=stop i=inject q=quit\n\n" + strings.Join(m.lines, "\n") + "\n"
}

func main() {
	m := &model{}
	p := tea.NewProgram(m)

	python, _ := exec.LookPath("python3")
	if python == "" {
		python, _ = exec.LookPath("python")
	}

	exe, _ := os.Executable()
	worker := filepath.Join(filepath.Dir(exe), "..", "main.py")

	w, err := protocol.Spawn(p.Send, python, worker)
	if err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
	m.worker = w

	p.Run()
}
