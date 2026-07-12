package handler

import (
	"bufio"
	"encoding/json"
	"os/exec"

	tea "github.com/charmbracelet/bubbletea"
)

type Envelope struct {
	Type string          `json:"type"`
	ID   string          `json:"id"`
	Data json.RawMessage `json:"data"`
}

type EventMsg Envelope

type StatusData struct {
	State string `json:"state"`
	Text  string `json:"text,omitempty"`
}

type CmdData struct {
	Action string `json:"action"`
	Text   string `json:"text,omitempty"`
}

type Worker struct {
	cmd    *exec.Cmd
	stdin  *bufio.Writer
	stdout *bufio.Scanner
}

func Spawn(send func(tea.Msg), path string, args ...string) (*Worker, error) {
	cmd := exec.Command(path, args...)
	stdinPipe, err := cmd.StdinPipe()
	if err != nil {
		return nil, err
	}
	stdoutPipe, err := cmd.StdoutPipe()
	if err != nil {
		return nil, err
	}
	if err := cmd.Start(); err != nil {
		return nil, err
	}

	w := &Worker{cmd: cmd, stdin: bufio.NewWriter(stdinPipe), stdout: bufio.NewScanner(stdoutPipe)}
	go w.readLoop(send)
	return w, nil
}

func (w *Worker) readLoop(send func(tea.Msg)) {
	for w.stdout.Scan() {
		var env Envelope
		if json.Unmarshal(w.stdout.Bytes(), &env) == nil {
			send(EventMsg(env))
		}
	}
}

func (w *Worker) Send(type_ string, data any) error {
	payload, err := json.Marshal(data)
	if err != nil {
		return err
	}
	line, err := json.Marshal(Envelope{Type: type_, Data: payload})
	if err != nil {
		return err
	}
	w.stdin.Write(line)
	w.stdin.WriteByte('\n')
	return w.stdin.Flush()
}

func (w *Worker) Pause() { w.Send("cmd", CmdData{Action: "pause"}) }
func (w *Worker) Stop()  { w.Send("cmd", CmdData{Action: "stop"}) }
func (w *Worker) Inject(text string) {
	w.Send("cmd", CmdData{Action: "inject", Text: text})
}
