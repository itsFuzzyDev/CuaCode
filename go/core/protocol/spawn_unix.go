//go:build !windows

package protocol

import (
	"os/exec"
	"syscall"
)

// detach gives the worker its own session, so it is not a job of the terminal
// the frontend is drawing in.
//
// It is a title fix as much as a signal one. A terminal names its tab after the
// deepest process holding its tty, and the worker is a python interpreter - on
// macOS a framework build whose executable is literally named "Python" - so the
// tab said "Python" no matter what the frontend set the title to. Out of the
// tty it is not a candidate for that name any more.
//
// The signal half is worth having on its own: a Ctrl-C typed at the terminal
// goes to its foreground process group, which used to include the worker. The
// frontend owns that key and answers it by cancelling the run; the worker had
// no business dying of it. It still cannot outlive the frontend - it exits on a
// closed pipe or a reparent - so nothing here orphans anything.
func detach(cmd *exec.Cmd) {
	cmd.SysProcAttr = &syscall.SysProcAttr{Setsid: true}
}
