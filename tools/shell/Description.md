---
name: shell
output:
  stdout: string
  stderr: string
  exit_code: number
active: True
require_permissions: True
backgroundable: True
---
Runs a command in the user's login shell and returns stdout, stderr, and the
exit code. Use it for anything the machine can do without the GUI — inspecting
files, git, package managers, build and test commands, scripts — and drive the
screen only when a task genuinely needs the interface.

The first command starts in the directory the user launched from, or their home
directory when the frontend has none to report. From there the working directory
persists between calls, so `cd` in one command still holds for the next; pass cwd
to set it explicitly (supports ~). Every result carries the directory it ran in.
timeout is in seconds (default 30, max 600); on expiry the whole process group is
killed and the call comes back with timeout: true.

Pass background: true for a command you do not want to sit through — a build, a
test suite, a long install. You get a job id straight back instead of the
output, the command keeps running, and you read the result later through the
background tool. Do not use it for anything whose answer decides your next
step; waiting is cheaper than a round spent asking whether the answer arrived
yet. The user can also push a command you are already waiting on into the
background, in which case a call you expected output from returns a job id.

A command that starts a GUI app gets that app parked on the right of the screen,
the same way app_open would have, and the result names it under opened_apps. Some
apps register too slowly to catch there; the next screenshot parks those. Use
app_open when opening an app is the whole point — this is only a safety net for a
launch that had to go through the shell.

stdin is closed, so a command that would prompt fails instead of hanging —
pass flags like -y rather than answering interactively. Output is capped and
the middle is dropped when it is too long, so filter noisy commands (head,
grep, tail) instead of returning everything. A command detached with a trailing
`&` must still redirect its output (`cmd > /tmp/out.log 2>&1 &`) or the call
waits on the open pipe until it times out — prefer background: true, which
keeps the output.

A cancelled run kills the command, so anything meant to outlive the turn wants
background: true rather than a bare `&`.

Commands run with the user's own privileges and are not undoable. Deleting or
overwriting data, installing software, and changing system state need the
user's go-ahead first, exactly as they do on screen.

Do not run interactiable commands or commmands that will return very large outputs.
Try splitting up your shell commands so that no problems occur with large outputs.