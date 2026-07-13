package protocol

import (
	"bufio"
	"encoding/json"
	"os/exec"

	tea "github.com/charmbracelet/bubbletea"
)

type EventMsg struct {
	Raw []byte
}

type Worker struct {
	stdin *bufio.Writer
	cmd   *exec.Cmd
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

	go func() {
		scanner := bufio.NewScanner(stdoutPipe)
		for scanner.Scan() {
			send(EventMsg{Raw: scanner.Bytes()})
		}
		_ = scanner.Err()
	}()

	return &Worker{stdin: bufio.NewWriter(stdinPipe), cmd: cmd}, nil
}

func (w *Worker) Kill() error {
	if w.cmd != nil && w.cmd.Process != nil {
		return w.cmd.Process.Kill()
	}
	return nil
}

func (w *Worker) SendRaw(line string) error {
	_, err := w.stdin.WriteString(line + "\n")
	if err != nil {
		return err
	}
	return w.stdin.Flush()
}

func (w *Worker) SendEnv(env Envelope) error {
	b, err := json.Marshal(env)
	if err != nil {
		return err
	}
	return w.SendRaw(string(b))
}
