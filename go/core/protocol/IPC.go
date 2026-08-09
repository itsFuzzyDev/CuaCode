package protocol

import (
	"bufio"
	"encoding/json"
	"os/exec"
)

// Worker is a line-delimited JSON subprocess. It knows nothing about any
// frontend: callers receive raw stdout lines via the onLine callback and
// write envelopes with SendEnv.
type Worker struct {
	stdin *bufio.Writer
	cmd   *exec.Cmd
}

// Spawn starts the worker process and streams each stdout line to onLine
// from a background goroutine. onLine must be safe to call concurrently
// with the caller's own goroutine.
func Spawn(onLine func(line []byte), path string, args ...string) (*Worker, error) {
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
			// Copy: scanner reuses its buffer, and onLine may hold the slice.
			line := make([]byte, len(scanner.Bytes()))
			copy(line, scanner.Bytes())
			onLine(line)
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
