package protocol

import (
	"bufio"
	"encoding/json"
	"os/exec"
	"strings"
	"sync"
	"sync/atomic"
)

// stderrTail is how much of the worker's stderr is kept for the exit report.
// Only the tail matters: a Python traceback ends with the line that explains
// it, and the frames above are noise on a one-line notice.
const stderrTail = 8 << 10

// maxLine is the longest single stdout line the reader accepts. bufio's own
// 64KiB default is smaller than a tool_calls payload can get, and a line over
// the limit stops the scanner silently -- which reads exactly like the worker
// dying, with nothing said about it.
const maxLine = 8 << 20

// tail is an io.Writer keeping only the last max bytes written to it.
type tail struct {
	mu  sync.Mutex
	buf []byte
	max int
}

func (t *tail) Write(p []byte) (int, error) {
	t.mu.Lock()
	defer t.mu.Unlock()
	t.buf = append(t.buf, p...)
	if len(t.buf) > t.max {
		t.buf = t.buf[len(t.buf)-t.max:]
	}
	return len(p), nil
}

func (t *tail) String() string {
	t.mu.Lock()
	defer t.mu.Unlock()
	return strings.TrimSpace(string(t.buf))
}

// Worker is a line-delimited JSON subprocess. It knows nothing about any
// frontend: callers receive raw stdout lines via the onLine callback and
// write envelopes with SendEnv.
type Worker struct {
	stdin *bufio.Writer
	cmd   *exec.Cmd
	// Whatever the worker last wrote to stderr. Captured rather than inherited
	// because a TUI frontend owns the screen: a traceback printed there is
	// painted over by the next frame, and the failure looks like silence.
	errs    *tail
	closing atomic.Bool
}

// Spawn starts the worker process and streams each stdout line to onLine
// from a background goroutine. onLine must be safe to call concurrently
// with the caller's own goroutine.
//
// A worker that dies is reported through the same callback, as a synthetic
// error status, so a frontend already drawing worker errors draws this one
// too instead of sitting at an empty screen forever.
func Spawn(onLine func(line []byte), path string, args ...string) (*Worker, error) {
	cmd := exec.Command(path, args...)
	// Out of the terminal's session before it is started — see detach.
	detach(cmd)
	stdinPipe, err := cmd.StdinPipe()
	if err != nil {
		return nil, err
	}
	stdoutPipe, err := cmd.StdoutPipe()
	if err != nil {
		return nil, err
	}
	w := &Worker{stdin: bufio.NewWriter(stdinPipe), cmd: cmd, errs: &tail{max: stderrTail}}
	// Set as a plain writer, not a pipe: exec then owns the copying goroutine
	// and Wait below already accounts for it.
	cmd.Stderr = w.errs
	if err := cmd.Start(); err != nil {
		return nil, err
	}

	go func() {
		scanner := bufio.NewScanner(stdoutPipe)
		scanner.Buffer(make([]byte, 0, 64<<10), maxLine)
		for scanner.Scan() {
			// Copy: scanner reuses its buffer, and onLine may hold the slice.
			line := make([]byte, len(scanner.Bytes()))
			copy(line, scanner.Bytes())
			onLine(line)
		}
		scanErr := scanner.Err()
		// Only safe here: Wait closes the stdout pipe, so it cannot be called
		// until every read off it has finished.
		waitErr := cmd.Wait()
		w.report(onLine, scanErr, waitErr)
	}()

	return w, nil
}

// report tells the frontend the worker is gone, unless we are the ones who
// killed it. Sent as a normal error status so no frontend needs new code to
// show it.
func (w *Worker) report(onLine func(line []byte), scanErr, waitErr error) {
	if onLine == nil || w.closing.Load() {
		return
	}
	msg := "worker exited"
	if waitErr != nil {
		msg += ": " + waitErr.Error()
	}
	if scanErr != nil {
		msg += ": reading worker output: " + scanErr.Error()
	}
	if out := w.errs.String(); out != "" {
		msg += "\n" + out
	}
	data, err := json.Marshal(map[string]string{"state": "error", "error": msg})
	if err != nil {
		return
	}
	line, err := json.Marshal(Envelope{Type: "status", ID: "worker-exit", Data: data})
	if err != nil {
		return
	}
	onLine(line)
}

// Stderr is whatever the worker last wrote to stderr, tail-first. For a
// frontend that wants to show more than the exit line carries.
func (w *Worker) Stderr() string { return w.errs.String() }

func (w *Worker) Kill() error {
	// Marked before the signal, so the exit this causes is not reported back
	// to a frontend that is already shutting down.
	w.closing.Store(true)
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
