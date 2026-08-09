import subprocess, time

# A window that just appeared is not a window that has finished appearing: apps
# animate windows open, show a splash first, and restore their saved frame a
# beat after launch. Every constant here exists to outlive one of those.
_STABLE_SAMPLES = 2       # identical geometry readings before a window counts as settled
_SAMPLE_GAP = 0.3         # seconds between those readings
_MIN_WINDOW_PX = 100      # smaller than this is a splash/tooltip, not the app
_SNAP_ATTEMPTS = 4        # move+resize passes before giving up
_SNAP_RETRY_GAP = 0.4
_LATE_MOVE_WAIT = 0.7     # grace period for an app to undo our move, so we can redo it
_TOLERANCE = 12           # px slack when checking the window actually landed


def _osa(script: str) -> tuple[int, str]:
    r = subprocess.run(["osascript", "-e", script], capture_output=True, text=True)
    return r.returncode, r.stdout.strip()


def _esc(name: str) -> str:
    return name.replace("\\", "\\\\").replace('"', '\\"')


def get_frontmost() -> str | None:
    try:
        rc, out = _osa('tell application "System Events" to get name of first process whose frontmost is true')
        return out or None
    except Exception:
        return None


# Pick the app's real window rather than trusting `window 1`. `window 1` is
# resolved fresh on every AppleScript statement, so a dialog or splash opening
# mid-snap can silently swap which window the second half of the snap moves.
_PICK_WINDOW = '''            set target to missing value
            repeat with ww in windows
                try
                    if (value of attribute "AXMinimized" of ww) is false then
                        set sz to size of ww
                        if (item 1 of sz) > {min_px} and (item 2 of sz) > {min_px} then
                            set target to ww
                            exit repeat
                        end if
                    end if
                end try
            end repeat
            if target is missing value then return "none"'''.replace("{min_px}", str(_MIN_WINDOW_PX))


def _geometry(proc_name: str) -> tuple[int, int, int, int] | None:
    """(x, y, w, h) of the app's real window, or None if it has none yet."""
    script = f'''tell application "System Events"
        tell process "{_esc(proc_name)}"
{_PICK_WINDOW}
            set p to position of target
            set s to size of target
            return ((item 1 of p) as text) & " " & ((item 2 of p) as text) & " " & ((item 1 of s) as text) & " " & ((item 2 of s) as text)
        end tell
    end tell'''
    try:
        rc, out = _osa(script)
        if rc != 0 or out == "none":
            return None
        x, y, w, h = [int(float(v)) for v in out.split()]
        return x, y, w, h
    except Exception:
        return None


def _wait_for_window(proc_name: str, timeout: float = 8) -> bool:
    """Wait until the app has a window whose geometry stops changing.

    The old check -- window count > 0 -- returned during the open animation, so
    the snap landed on a window the app was still positioning. The app then put
    it back wherever it wanted, which is why a snapped app sometimes ended up
    centered."""
    deadline = time.time() + timeout
    last, stable = None, 0
    while time.time() < deadline:
        geo = _geometry(proc_name)
        if geo and geo == last:
            stable += 1
            if stable >= _STABLE_SAMPLES:
                return True
        else:
            stable = 0
        last = geo
        time.sleep(_SAMPLE_GAP)
    return last is not None


def open_app(app: str) -> str | None:
    try:
        subprocess.run(["open", "-a", app], check=True)
    except subprocess.CalledProcessError:
        subprocess.run(["open", app], check=True)

    deadline = time.time() + 12
    proc_name = None
    while time.time() < deadline:
        time.sleep(0.4)
        rc, out = _osa('tell application "System Events" to get name of every application process whose visible is true')
        names = [n.strip() for n in out.split(",") if n.strip()]
        for n in names:
            if n.lower() == app.lower():
                proc_name = n
                break
        if proc_name: break

    if proc_name and _wait_for_window(proc_name):
        return proc_name
    return None


def _screen_size() -> tuple[int, int]:
    rc, out = _osa('tell application "Finder" to get bounds of window of desktop')
    _, _, sw, sh = [int(v.strip()) for v in out.split(",")]
    return sw, sh


def _leave_fullscreen(proc_name: str) -> bool:
    """A native-fullscreen window ignores position/size while reporting success."""
    script = f'''tell application "System Events"
        tell process "{_esc(proc_name)}"
            try
                if (value of attribute "AXFullScreen" of window 1) is true then
                    set value of attribute "AXFullScreen" of window 1 to false
                    return "exited"
                end if
            end try
            return "no"
        end tell
    end tell'''
    rc, out = _osa(script)
    return out == "exited"


def _apply(proc_name: str, x0: int, y0: int, w: int, h: int, focus: bool) -> tuple[int, int, int, int] | None:
    """One move+resize pass, geometry read back in the same AppleScript call.

    Size is set before position: moving a window that is still full-width can
    push its right edge off-screen, and apps clamp that by refusing the move --
    leaving the window parked mid-screen. Size again afterwards because some
    apps re-layout on move."""
    raise_line = "            set frontmost to true\n" if focus else ""
    script = f'''tell application "System Events"
        tell process "{_esc(proc_name)}"
{raise_line}{_PICK_WINDOW}
            set size of target to {{{w}, {h}}}
            set position of target to {{{x0}, {y0}}}
            set size of target to {{{w}, {h}}}
            set p to position of target
            set s to size of target
            return ((item 1 of p) as text) & " " & ((item 2 of p) as text) & " " & ((item 1 of s) as text) & " " & ((item 2 of s) as text)
        end tell
    end tell'''
    try:
        rc, out = _osa(script)
        if rc != 0 or out == "none":
            return None
        x, y, ww, hh = [int(float(v)) for v in out.split()]
        return x, y, ww, hh
    except Exception:
        return None


def _landed(geo: tuple[int, int, int, int] | None, x0: int, y0: int) -> bool:
    # Position only. An app with a minimum width cannot be squeezed into the
    # slice, and that is fine -- being in the wrong half of the screen is not.
    return geo is not None and abs(geo[0] - x0) <= _TOLERANCE and abs(geo[1] - y0) <= _TOLERANCE


def snap_region(proc_name: str | None, x_start_frac: float, x_end_frac: float, focus: bool = False) -> bool:
    """Move/resize a process's real window into a horizontal slice of the screen.

    Returns True only after reading the window back and confirming it is where
    it was asked to be -- osascript exits 0 whether or not the app honoured the
    geometry, so the return code proves nothing.

    focus defaults to False: System Events can reposition a window without
    raising it, and whoever calls this last would otherwise decide what the
    keyboard and mouse are pointed at. Only the caller that genuinely wants
    the window in front should ask for it."""
    if not proc_name: return False
    try:
        sw, sh = _screen_size()
        x0, x1 = int(sw * x_start_frac), int(sw * x_end_frac)
        w, h = x1 - x0, sh

        if _leave_fullscreen(proc_name):
            time.sleep(1.0)  # the exit is animated; geometry set during it is discarded

        for attempt in range(_SNAP_ATTEMPTS):
            geo = _apply(proc_name, x0, 0, w, h, focus)
            if _landed(geo, x0, 0):
                # Apps that restore a saved frame do it shortly after launch,
                # i.e. after a snap that just verified clean. Look once more.
                time.sleep(_LATE_MOVE_WAIT)
                if _landed(_geometry(proc_name), x0, 0):
                    return True
                continue
            time.sleep(_SNAP_RETRY_GAP)

        # Last pass, reported honestly.
        return _landed(_apply(proc_name, x0, 0, w, h, focus), x0, 0)
    except Exception:
        return False
