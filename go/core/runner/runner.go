// Package runner is the boilerplate every frontend shares: find the Python
// interpreter, find the worker script, and hand back a started Session.
//
// A new frontend only needs:
//
//	sess, err := runner.Start(func(ev session.Event) { ... })
//	defer sess.Close()
package runner

import (
	"fmt"
	"os"
	"os/exec"
	"os/signal"
	"path/filepath"
	"syscall"

	"cuacode/core/session"
)

// WorkerEnv overrides worker script discovery when set.
const WorkerEnv = "CUACODE_WORKER"

// Python returns the interpreter to run the worker with. CUACODE_PYTHON wins,
// then a venv next to the worker script, then python3/python on PATH.
func Python(workerPath string) (string, error) {
	if p := os.Getenv("CUACODE_PYTHON"); p != "" {
		return p, nil
	}
	if workerPath != "" {
		venv := filepath.Join(filepath.Dir(workerPath), "venv", "bin", "python3")
		if st, err := os.Stat(venv); err == nil && !st.IsDir() {
			return venv, nil
		}
	}
	for _, name := range []string{"python3", "python"} {
		if p, err := exec.LookPath(name); err == nil {
			return p, nil
		}
	}
	return "", fmt.Errorf("no python interpreter found on PATH (set CUACODE_PYTHON)")
}

// WorkerPath locates the Python worker (main.py at the repo root). It checks
// $CUACODE_WORKER, then walks up from the working directory, then from the
// executable, so `go run ./frontends/x` and a built binary both work.
func WorkerPath() (string, error) {
	if p := os.Getenv(WorkerEnv); p != "" {
		if _, err := os.Stat(p); err != nil {
			return "", fmt.Errorf("%s=%s: %w", WorkerEnv, p, err)
		}
		return p, nil
	}

	var roots []string
	if wd, err := os.Getwd(); err == nil {
		roots = append(roots, wd)
	}
	if exe, err := os.Executable(); err == nil {
		if resolved, err := filepath.EvalSymlinks(exe); err == nil {
			exe = resolved
		}
		roots = append(roots, filepath.Dir(exe))
	}

	for _, dir := range roots {
		for {
			cand := filepath.Join(dir, "main.py")
			if st, err := os.Stat(cand); err == nil && !st.IsDir() {
				return cand, nil
			}
			parent := filepath.Dir(dir)
			if parent == dir {
				break
			}
			dir = parent
		}
	}
	return "", fmt.Errorf("main.py not found above cwd or executable (set %s)", WorkerEnv)
}

// Start locates the worker, spawns it, and returns the live Session. notify is
// called from the worker's reader goroutine — frontends should forward the
// event to their own loop rather than touch UI state directly.
//
// It also installs a SIGTERM/SIGHUP handler that closes the session, so the
// worker never outlives the frontend.
func Start(notify func(session.Event)) (*session.Session, error) {
	return StartWith(notify, session.Options{TerminalInfo: session.ProbeTerminal})
}

// StartWith is Start with explicit session options, for frontends that are not
// terminals (GUIs) or that want to report their own TerminalInfo.
func StartWith(notify func(session.Event), opts session.Options) (*session.Session, error) {
	worker, err := WorkerPath()
	if err != nil {
		return nil, err
	}
	python, err := Python(worker)
	if err != nil {
		return nil, err
	}

	sess := session.New(notify, opts)
	if err := sess.Start(python, worker); err != nil {
		return nil, fmt.Errorf("start worker %s: %w", worker, err)
	}

	go func() {
		c := make(chan os.Signal, 1)
		signal.Notify(c, syscall.SIGTERM, syscall.SIGHUP)
		<-c
		sess.Close()
		os.Exit(1)
	}()

	return sess, nil
}
