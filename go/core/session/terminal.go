package session

import (
	"os"
	"os/exec"
	"runtime"
	"strings"

	"cuacode/core/protocol"
)

// ProbeTerminal gathers terminal environment info for Options.TerminalInfo.
// Meant for terminal frontends (TUI/CLI); GUI frontends supply their own.
func ProbeTerminal() protocol.TerminalData {
	t := protocol.TerminalData{
		TERM:         os.Getenv("TERM"),
		FrontmostApp: frontmostApp(),
	}
	if p := os.Getenv("TERM_PROGRAM"); p != "" {
		t.Program = p
	}
	if out, err := exec.Command("tty").Output(); err == nil {
		t.TTY = strings.TrimSpace(string(out))
	}
	t.CWD = WorkingDir()
	return t
}

// WorkingDir is the directory the user was standing in, which is not always the
// process's own. CUACODE_CWD wins: run.sh launches the frontend through
// `go run`, which executes it from the module directory, so the process cwd is
// go/ rather than wherever the user was. Set by that script, absent for a real
// binary.
//
// Anything a frontend shows the user about "here" — a file picker, a path — has
// to agree with what the worker was told, so both read it from here.
func WorkingDir() string {
	if wd := os.Getenv("CUACODE_CWD"); wd != "" {
		return wd
	}
	if wd, err := os.Getwd(); err == nil {
		return wd
	}
	return "."
}

func frontmostApp() string {
	switch runtime.GOOS {
	case "darwin":
		out, err := exec.Command("osascript", "-e",
			`tell application "System Events" to get name of first process whose frontmost is true`).Output()
		if err == nil {
			return strings.TrimSpace(string(out))
		}
	case "windows":
		ps := `Add-Type -MemberDefinition '[DllImport("user32.dll")]public static extern IntPtr GetForegroundWindow();' -Name NativeMethods -PassThru | ForEach-Object { $hwnd = $_.GetForegroundWindow(); (Get-Process | Where-Object { $_.MainWindowHandle -eq $hwnd }).ProcessName }`
		out, err := exec.Command("powershell", "-NoProfile", "-Command", ps).Output()
		if err == nil {
			return strings.TrimSpace(string(out))
		}
	default: // linux
		out, err := exec.Command("xdotool", "getactivewindow", "getwindowname").Output()
		if err == nil {
			return strings.TrimSpace(string(out))
		}
	}
	return ""
}
