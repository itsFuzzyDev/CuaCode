import os, platform, signal, subprocess, threading, time
from pathlib import Path

from tools._safety import shell as safety

OUT_CAP = 20_000        # chars per stream, keeps a runaway command out of the model's context
DEFAULT_TIMEOUT = 30
# The loop watches this call from another thread now, so a long timeout no
# longer means a long stretch where nothing can be stopped -- the ceiling is
# only about how long a command is allowed to be *wrong* for before it is cut
# off, not about how long the app is deaf.
MAX_TIMEOUT = 600
MARK = "<<cuacode-cwd>>"

WINDOWS = platform.system() == "Windows"

# Survives between calls: `cd` in one command still holds for the next, which
# is what the model assumes a shell does. Resolved on first use rather than at
# import, because the frontend only reports its terminal after the worker is up.
_cwd = None

def _start_dir(ctx) -> str:
    """The directory the user launched from, when there is one.

    Not the worker's own cwd: that is wherever the frontend process happened to
    be, and for a GUI launch it is / or an app bundle. A frontend that has no
    meaningful directory sends none, and home is the honest answer there.
    """
    d = (ctx or {}).get("cwd") if hasattr(ctx, "get") else None
    if d and Path(d).is_dir(): return str(Path(d).resolve())
    return str(Path.home())

def _argv(command: str) -> list[str]:
    """The command, plus a trailer that reports where it left off.

    The status is saved before the trailer runs and restored after it, or the
    printf would be the last command in the shell and every failure would come
    back as a success.

    A login shell, not a plain one: launched from an app bundle the worker
    inherits a bare PATH, and every tool the user installed would be missing
    from a shell that skips their profile.
    """
    if WINDOWS:
        return ["powershell", "-NoProfile", "-Command",
                f"{command}\n$__rc = $LASTEXITCODE; if ($null -eq $__rc) {{ $__rc = if ($?) {{ 0 }} else {{ 1 }} }}\n"
                f"Write-Output \"{MARK}$($PWD.Path)\"\nexit $__rc"]
    return [os.environ.get("SHELL") or "/bin/sh", "-lc",
            f"{command}\n__rc=$?; printf '\\n%s%s' '{MARK}' \"$PWD\"; exit $__rc"]

def _split_cwd(out: str) -> tuple[str, str | None]:
    """Take the trailer back off. It is absent whenever the command ended the
    shell itself -- `exit 1`, a kill on timeout -- and then the directory is
    simply left where it was."""
    i = out.rfind(MARK)
    if i < 0: return out, None
    return out[:i].rstrip("\n"), out[i + len(MARK):].strip()

def _cap(s: str) -> tuple[str, bool]:
    """Head and tail both: a failing build puts the reason at the end and the
    context at the start, and keeping only one of them loses the failure."""
    if len(s) <= OUT_CAP: return s, False
    head, tail = s[:OUT_CAP * 2 // 3], s[-OUT_CAP // 3:]
    return f"{head}\n... [{len(s) - len(head) - len(tail)} chars omitted] ...\n{tail}", True

def _kill(proc):
    """The whole process group. Killing just the shell orphans whatever it
    spawned, which then holds our pipe open and hangs the read that follows."""
    try:
        if WINDOWS: proc.kill()
        else: os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        proc.kill()

def _collect(proc, timeout: float, cancel) -> tuple[str, str, str]:
    """Read the command to the end, while staying interruptible.

    It has to be communicate(), and communicate() blocks -- but the pipes are
    the reason a plain poll loop is not an option. A command that fills the pipe
    buffer blocks in its own write() until somebody drains it, so waiting on the
    process without reading it is not a slow wait, it is a deadlock. The read
    goes on a thread and the watching happens here.

    cancel is the token the agent loop puts in ctx. Honouring it is what makes
    stopping the *run* also stop the command: without it the shell keeps going
    after the turn that asked for it is gone, holding whatever it was holding.

    Returns (stdout, stderr, why) where why is ok | timeout | cancelled.
    """
    box: dict = {}

    def drain():
        try: box["io"] = proc.communicate()
        except Exception: box["io"] = ("", "")
        finally: box["done"] = True

    t = threading.Thread(target=drain, daemon=True)
    t.start()
    deadline = time.monotonic() + timeout
    why = "ok"
    while True:
        t.join(0.05)
        if box.get("done"): break
        if cancel is not None and cancel.cancelled(): why = "cancelled"; break
        if time.monotonic() >= deadline: why = "timeout"; break
    if why != "ok":
        _kill(proc)
        # Killing the group closes the pipes, so the drain returns on its own;
        # the bound is only there so a wedged read can never hold this thread.
        t.join(5.0)
    out, err = box.get("io") or ("", "")
    return out, err, why


def safe(args: dict, ctx) -> bool:
    """Whether this command can run without asking.

    Only the ones that look: see tools/_safety/shell.py, which reads the
    command the way the shell will and says no to everything it cannot account
    for. `date && whoami && ls -la | grep a` is three lookups and a filter and
    has nothing to approve; `rm -rf build` is one word longer and does not come
    back. cwd is not consulted -- where a read-only command runs does not make
    it less read-only.
    """
    return safety.is_read_only(args.get("command") or "")[0]

def run(args: dict, ctx) -> dict:
    global _cwd
    command = (args.get("command") or "").strip()
    if not command: return {"error": "command required"}

    if _cwd is None: _cwd = _start_dir(ctx)
    if args.get("cwd"):
        d = Path(args["cwd"]).expanduser()
        if not d.is_dir(): return {"error": f"not a directory: {d}"}
        _cwd = str(d.resolve())

    timeout = max(1.0, min(float(args.get("timeout") or DEFAULT_TIMEOUT), MAX_TIMEOUT))
    started = time.monotonic()
    # stdin closed: a command that would prompt fails immediately instead of
    # waiting on input nobody is there to give it.
    proc = subprocess.Popen(_argv(command), cwd=_cwd, stdin=subprocess.DEVNULL,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True, errors="replace",
                            **({} if WINDOWS else {"start_new_session": True}))
    out, err, why = _collect(proc, timeout, ctx.get("cancel") if hasattr(ctx, "get") else None)

    out, ended_in = _split_cwd(out)
    if ended_in and Path(ended_in).is_dir(): _cwd = ended_in
    out, cut_out = _cap(out)
    err, cut_err = _cap(err)

    result = {"cwd": _cwd, "exit_code": proc.returncode, "stdout": out, "stderr": err,
              "duration": round(time.monotonic() - started, 2)}
    if cut_out or cut_err: result["truncated"] = True
    if why == "timeout": result["timeout"] = True
    # Reported rather than raised, because a cancelled call still has whatever
    # the command printed before it was cut off, and a backgrounded one that is
    # later killed is read back through exactly this dict.
    if why == "cancelled": result["cancelled"] = True
    return result
