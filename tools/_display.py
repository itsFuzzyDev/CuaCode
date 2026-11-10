"""The environment the graphical session runs with, for a process that lacks it.

A worker started over ssh inherits the login's environment, not the desktop's: no
DISPLAY, no session bus, no X authority. Every X tool then fails as though the machine
had no screen at all, and the screenshot portal cannot be reached. This fills those in
from the login user's own session, and leaves the environment alone when it is already
complete, which is the case for a worker started inside the desktop.

Filled into os.environ as well as returned, because libraries read the process
environment directly: mss cannot be handed an env dict the way a subprocess can.
"""

import glob, os, subprocess

def _runtime_dir() -> str | None:
    d = os.environ.get("XDG_RUNTIME_DIR")
    if d and os.path.isdir(d): return d
    # The login user's own runtime directory, which exists whenever a session does.
    d = f"/run/user/{os.getuid()}"
    return d if os.path.isdir(d) else None

def _xauth(runtime: str) -> str | None:
    a = os.environ.get("XAUTHORITY")
    if a and os.path.exists(a): return a
    # XWayland's cookie, written at login with a random suffix.
    hits = sorted(glob.glob(os.path.join(runtime, ".mutter-Xwaylandauth.*")))
    if hits: return hits[0]
    a = os.path.expanduser("~/.Xauthority")
    return a if os.path.exists(a) else None

def _fill(key: str, value: str | None):
    # Empty counts as missing: ssh forwards some of these as "" instead of dropping them.
    if value and not os.environ.get(key): os.environ[key] = value

def env() -> dict:
    """os.environ, with the session's display, bus and authority filled in."""
    runtime = _runtime_dir()
    if runtime:
        _fill("XDG_RUNTIME_DIR", runtime)
        bus = os.path.join(runtime, "bus")
        if os.path.exists(bus): _fill("DBUS_SESSION_BUS_ADDRESS", f"unix:path={bus}")
        _fill("XAUTHORITY", _xauth(runtime))
    if not os.environ.get("DISPLAY") and os.path.isdir("/tmp/.X11-unix"):
        socks = sorted(os.listdir("/tmp/.X11-unix"))
        if socks: _fill("DISPLAY", ":" + socks[0].lstrip("X"))
    return dict(os.environ)

def cursor() -> tuple[int, int] | None:
    """Where the pointer actually is, or None when it cannot be read.

    tools/_pointer.py reads this back after a move to catch one that was dropped
    rather than retried blind. A Wayland compositor ignores motion from X clients, so
    on those the answer is "still where it was", which is exactly the useful answer.
    """
    try:
        r = subprocess.run(["xdotool", "getmouselocation", "--shell"], capture_output=True,
                           text=True, env=env(), timeout=5)
        d = dict(l.split("=", 1) for l in r.stdout.strip().splitlines() if "=" in l)
        return int(d["X"]), int(d["Y"])
    except Exception:
        return None
