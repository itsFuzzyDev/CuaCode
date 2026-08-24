//go:build windows

package protocol

import "os/exec"

// detach is a no-op on Windows: there is no controlling terminal to leave, and
// a console process that detaches from its parent's console loses the pipes
// with it.
func detach(cmd *exec.Cmd) {}
