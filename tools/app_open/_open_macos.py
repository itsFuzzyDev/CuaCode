import subprocess, time

# A window that just appeared has not finished appearing: apps animate, splash,
# and restore saved frames late. Every constant below outlives one of those.
# Reads are ~1ms through the Accessibility API; they were 180ms of osascript,
# which is why the gaps used to be sized for call count instead of animation.
_STABLE_SAMPLES = 2       # identical geometry readings before a window counts as settled
_SAMPLE_GAP = 0.08        # seconds between those readings
_MIN_WINDOW_PX = 100      # smaller than this is a splash/tooltip, not the app
_SNAP_ATTEMPTS = 4        # move+resize passes before giving up
_SNAP_RETRY_GAP = 0.15
_SETTLE = 0.06            # after writing geometry, before reading it back
_LATE_MOVE_WAIT = 0.7     # grace period for an app to undo our move, so we can redo it
_TOLERANCE = 12           # px slack when checking the window actually landed


# ---------------------------------------------------------------- AX backend

# Loaded once, on first use. False means this machine cannot answer through the
# Accessibility API -- pyobjc missing, or the app was never granted Accessibility
# permission -- and every call below falls back to the AppleScript it replaces.
_AX = None


def _ax():
    """The Accessibility entry points, or None if they are not usable here."""
    global _AX
    if _AX is None:
        try:
            from types import SimpleNamespace
            import Quartz
            from tools._appkit import appkit
            AppKit = appkit()
            from ApplicationServices import (
                AXUIElementCreateApplication, AXUIElementCopyAttributeValue,
                AXUIElementSetAttributeValue, AXValueCreate, AXValueGetValue,
                kAXValueCGPointType, kAXValueCGSizeType)
            _AX = SimpleNamespace(
                ws=AppKit.NSWorkspace.sharedWorkspace(), AppKit=AppKit, Q=Quartz,
                create=AXUIElementCreateApplication, get=AXUIElementCopyAttributeValue,
                set=AXUIElementSetAttributeValue, mk=AXValueCreate, read=AXValueGetValue,
                POINT=kAXValueCGPointType, SIZE=kAXValueCGSizeType)
        except Exception:
            _AX = False
    return _AX or None


_APPS = {}          # pid -> AXUIElement, so the app handle is built once


def _pump():
    """Let the workspace hear about launches that happened since the last look.

    NSWorkspace's list of running applications is not a query, it is a cache
    kept up to date by notifications -- and notifications are delivered on a run
    loop. The agent's worker has none: it starts, imports this, and then spends
    its life blocking on a socket. So runningApplications() answered with the
    process list as it stood at import for the whole session, and an app the
    agent launched a minute later was invisible to every lookup here -- which is
    the whole of why an app opened from the shell was never seen, never snapped,
    and reported as "no such app" when asked about directly.

    Draining the loop with a zero timeout costs nothing when there is nothing
    queued, which is the common case.
    """
    ax = _ax()
    if not ax: return
    try:
        ax.Q.CFRunLoopRunInMode(ax.Q.kCFRunLoopDefaultMode, 0, False)
    except Exception:
        pass


def _running(name: str):
    """The NSRunningApplication whose name matches, or None.

    localizedName is what `System Events` reports as the process name, which is
    what every caller here passes around. The bundle's own name is checked too:
    an app launched by bundle id or by path can be running under a display name
    the caller never saw.
    """
    ax = _ax()
    if not ax: return None
    _pump()
    want = name.lower()
    for app in ax.ws.runningApplications():
        if (app.localizedName() or "").lower() == want: return app
        url = app.bundleURL()
        if url and url.lastPathComponent().rsplit(".", 1)[0].lower() == want: return app
    return None


def _element(name: str):
    """The app's AX handle, cached by pid.

    Cached because building it is the only part that is not free; the *window*
    is deliberately re-fetched on every call, since a handle to a window that has
    since closed reads back stale geometry rather than failing.
    """
    app = _running(name)
    if not app: return None
    pid = app.processIdentifier()
    if pid not in _APPS:
        ax = _ax()
        _APPS[pid] = ax.create(pid)
    return _APPS[pid]


def _attr(el, name):
    """One attribute, or None. pyobjc returns (err, value); a non-zero err is
    every reason this can fail -- no such attribute, app not responding, and
    Accessibility permission denied all arrive the same way."""
    try:
        err, val = _ax().get(el, name, None)
        return None if err else val
    except Exception:
        return None


def _window(name: str):
    """The app's real window, picked the same way the AppleScript did.

    Not `window 1`: a dialog or splash opening mid-snap changes what window 1
    means between two statements, which is how the second half of a snap ended
    up moving something other than what the first half sized.
    """
    el = _element(name)
    if el is None: return None
    wins = _attr(el, "AXWindows")
    if not wins: return None
    ax = _ax()
    for w in wins:
        if _attr(w, "AXMinimized"): continue
        sv = _attr(w, "AXSize")
        if sv is None: continue
        ok, s = ax.read(sv, ax.SIZE, None)
        if ok and s.width > _MIN_WINDOW_PX and s.height > _MIN_WINDOW_PX:
            return w
    return None


def _ax_geometry(name: str):
    w = _window(name)
    if w is None: return None
    ax = _ax()
    pv, sv = _attr(w, "AXPosition"), _attr(w, "AXSize")
    if pv is None or sv is None: return None
    okp, p = ax.read(pv, ax.POINT, None)
    oks, s = ax.read(sv, ax.SIZE, None)
    if not (okp and oks): return None
    return int(p.x), int(p.y), int(s.width), int(s.height)


def _ax_apply(name: str, x0: int, y0: int, w: int, h: int, focus: bool):
    win = _window(name)
    if win is None: return None
    ax = _ax()
    if focus:
        app = _running(name)
        if app: app.activateWithOptions_(ax.AppKit.NSApplicationActivateIgnoringOtherApps)
    size = ax.mk(ax.SIZE, ax.Q.CGSize(w, h))
    # Size before position: moving a window that is still full-width pushes its
    # right edge off-screen, and apps clamp that by refusing the move outright --
    # leaving the window parked mid-screen. Size again after, because some apps
    # re-lay-out on move and take the height back.
    ax.set(win, "AXSize", size)
    ax.set(win, "AXPosition", ax.mk(ax.POINT, ax.Q.CGPoint(x0, y0)))
    ax.set(win, "AXSize", size)
    # These writes are messages to another process. Reading straight back reports
    # where the window was, not where it is going to be, and the retry loop then
    # spends a pass correcting something that was already correct.
    time.sleep(_SETTLE)
    return _ax_geometry(name)


def _ax_leave_fullscreen(name: str) -> bool:
    win = _window(name)
    if win is None: return False
    if not _attr(win, "AXFullScreen"): return False
    try:
        _ax().set(win, "AXFullScreen", False)
        return True
    except Exception:
        return False


# --------------------------------------------------- AppleScript fallback

def _osa(script: str) -> tuple[int, str]:
    r = subprocess.run(["osascript", "-e", script], capture_output=True, text=True)
    return r.returncode, r.stdout.strip()


def _esc(name: str) -> str:
    return name.replace("\\", "\\\\").replace('"', '\\"')


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


def _osa_geometry(proc_name: str):
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
        if rc != 0 or out == "none": return None
        x, y, w, h = [int(float(v)) for v in out.split()]
        return x, y, w, h
    except Exception:
        return None


def _osa_apply(proc_name: str, x0: int, y0: int, w: int, h: int, focus: bool):
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
        if rc != 0 or out == "none": return None
        x, y, ww, hh = [int(float(v)) for v in out.split()]
        return x, y, ww, hh
    except Exception:
        return None


def _osa_leave_fullscreen(proc_name: str) -> bool:
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


# ----------------------------------------------------------------- dispatch

def _geometry(proc_name: str) -> tuple[int, int, int, int] | None:
    """(x, y, w, h) of the app's real window, or None if it has none yet."""
    if _ax():
        geo = _ax_geometry(proc_name)
        if geo is not None: return geo
        # An app AX will not answer for at all is not the same as one with no
        # window yet, and only the fallback can tell the difference.
        if _running(proc_name) is None: return None
    return _osa_geometry(proc_name)


def _apply(proc_name: str, x0: int, y0: int, w: int, h: int, focus: bool):
    """One move+resize pass, geometry read back afterwards."""
    if _ax():
        geo = _ax_apply(proc_name, x0, y0, w, h, focus)
        if geo is not None: return geo
    return _osa_apply(proc_name, x0, y0, w, h, focus)


def _leave_fullscreen(proc_name: str) -> bool:
    """A native-fullscreen window ignores position/size while reporting success."""
    if _ax() and _window(proc_name) is not None:
        return _ax_leave_fullscreen(proc_name)
    return _osa_leave_fullscreen(proc_name)


def get_frontmost() -> str | None:
    ax = _ax()
    if ax:
        try:
            app = ax.ws.frontmostApplication()
            if app and app.localizedName(): return app.localizedName()
        except Exception:
            pass
    try:
        rc, out = _osa('tell application "System Events" to get name of first process whose frontmost is true')
        return out or None
    except Exception:
        return None


def _menu_bar() -> int:
    """Height of the menu bar, in points, or 0 if it cannot be measured.

    Needed because a snap asks for y=0 and most apps refuse it: AppKit clamps a
    window to below the menu bar and reports the clamped position back. Checking
    that against y=0 with 12px of slack said "did not land" for every one of
    them -- so every snap burned all four attempts and then returned False,
    which is both a second of latency and a lie about what happened.
    """
    ax = _ax()
    if not ax: return 0
    try:
        s = ax.AppKit.NSScreen.mainScreen()
        f, v = s.frame(), s.visibleFrame()
        return max(0, int(f.size.height - (v.origin.y + v.size.height)))
    except Exception:
        return 0


def _launch(app: str):
    """Start the app, by name, by bundle id, or by path -- and by nothing else.

    The fallback here used to be a bare `open <app>`, which is not a second way
    of naming an application: it is `open` treating the string as a path or a
    URL and handing it to whatever the system has registered as the default
    handler for it. A name macOS did not recognise therefore did not fail, it
    launched something -- Python Launcher.app for anything that looked like a
    .py, a browser for anything that looked like a URL, Finder for a directory.
    Being told "no such application" is strictly better than being handed an
    unrelated program and left to drive it.
    """
    if subprocess.run(["open", "-a", app], capture_output=True).returncode == 0: return
    # A bundle id, which is the other thing callers legitimately pass. Checked
    # by shape rather than tried blindly, so a plain name never reaches it.
    if "." in app and "/" not in app and " " not in app:
        if subprocess.run(["open", "-b", app], capture_output=True).returncode == 0: return
    # A path, and only if it is really there and really an application.
    from pathlib import Path
    p = Path(app).expanduser()
    if p.suffix == ".app" and p.exists():
        if subprocess.run(["open", str(p)], capture_output=True).returncode == 0: return
    raise RuntimeError(
        f"no application named {app!r} is installed. Pass the name as it appears in "
        "/Applications (or a bundle id), and use app_list to see what is available -- "
        "do not retry this name")


def open_app(app: str) -> str | None:
    # Asked before launching: `open -a` on an app that is already up is a no-op
    # that still costs a process spawn, and the poll below used to sleep 0.4s
    # before its first look regardless -- so re-focusing a running app paid the
    # full launch wait every time.
    running = _running(app)
    proc_name = running.localizedName() if running else None

    if not proc_name:
        _launch(app)

        deadline = time.time() + 12
        while time.time() < deadline:
            running = _running(app)
            if running:
                proc_name = running.localizedName()
                break
            if not _ax():
                rc, out = _osa('tell application "System Events" to get name of every application process whose visible is true')
                for n in (x.strip() for x in out.split(",")):
                    if n.lower() == app.lower():
                        proc_name = n
                        break
                if proc_name: break
            time.sleep(0.1)

    if proc_name and _wait_for_window(proc_name):
        return proc_name
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


def _screen_size() -> tuple[int, int]:
    ax = _ax()
    if ax:
        try:
            b = ax.Q.CGDisplayBounds(ax.Q.CGMainDisplayID())
            if b.size.width and b.size.height:
                return int(b.size.width), int(b.size.height)
        except Exception:
            pass
    rc, out = _osa('tell application "Finder" to get bounds of window of desktop')
    _, _, sw, sh = [int(v.strip()) for v in out.split(",")]
    return sw, sh


def _landed(geo: tuple[int, int, int, int] | None, x0: int, y0: int) -> bool:
    # Position only: an app that refuses to shrink is fine, being in the wrong
    # half is not. y=0 is checked against a range -- some apps clamp it to the
    # menu bar, and both are the top of the screen as far as this is concerned.
    if geo is None: return False
    if abs(geo[0] - x0) > _TOLERANCE: return False
    if y0 == 0: return -_TOLERANCE <= geo[1] <= _menu_bar() + _TOLERANCE
    return abs(geo[1] - y0) <= _TOLERANCE


# What each (process, target slice) pair actually settled at, last verified.
# Compared against verified geometry, not the request: an app clamped to a
# minimum width never matches its requested size, and re-deriving "correct"
# from the request would call that drift and move it forever.
_SETTLED = {}


def snap_region(proc_name: str | None, x_start_frac: float, x_end_frac: float, focus: bool = False) -> bool:
    """Move/resize a process's real window into a horizontal slice of the screen.

    Returns True only after reading the window back and confirming it is where
    it was asked to be -- the window server accepts a geometry write whether or
    not the app honours it, so a successful write proves nothing.

    focus defaults to False: a window can be repositioned without being raised,
    and whoever calls this last would otherwise decide what the keyboard and
    mouse are pointed at. Only the caller that genuinely wants the window in
    front should ask for it."""
    if not proc_name: return False
    try:
        sw, sh = _screen_size()
        x0, x1 = int(sw * x_start_frac), int(sw * x_end_frac)
        w, h = x1 - x0, sh
        key = (proc_name, x0, w, h)

        # Already parked, and nothing has touched it since. One geometry read,
        # no writes, no waiting for an app to re-lay-out something that is
        # already where it belongs.
        settled = _SETTLED.get(key)
        if settled is not None and _geometry(proc_name) == settled:
            if focus:
                app = _running(proc_name)
                if app: app.activateWithOptions_(_ax().AppKit.NSApplicationActivateIgnoringOtherApps)
                else: _apply(proc_name, x0, 0, w, h, True)
            return True

        if _leave_fullscreen(proc_name):
            time.sleep(1.0)  # the exit is animated; geometry set during it is discarded

        for attempt in range(_SNAP_ATTEMPTS):
            geo = _apply(proc_name, x0, 0, w, h, focus)
            if _landed(geo, x0, 0):
                # Apps that restore a saved frame do it shortly after launch,
                # i.e. after a snap that just verified clean. Look once more.
                time.sleep(_LATE_MOVE_WAIT)
                final = _geometry(proc_name)
                if _landed(final, x0, 0):
                    _SETTLED[key] = final
                    return True
                continue
            time.sleep(_SNAP_RETRY_GAP)

        # Last pass, reported honestly.
        final = _apply(proc_name, x0, 0, w, h, focus)
        if _landed(final, x0, 0):
            _SETTLED[key] = final
            return True
        _SETTLED.pop(key, None)
        return False
    except Exception:
        return False


def gui_apps() -> dict[str, str]:
    """{process name: the same name} for every app with a Dock presence.

    The value is what snap_region takes here, which on macOS is the process
    name -- callers hold a handle without caring what a handle is on this
    platform.

    Activation policy is the filter, not the window count: an app that is still
    drawing its first window is exactly the app a caller wants to hear about,
    while agents, daemons and menu bar extras never get a window at all and
    would otherwise be reported as newly launched apps forever.
    """
    ax = _ax()
    if ax:
        try:
            _pump()
            out = {}
            for app in ax.ws.runningApplications():
                # NSApplicationActivationPolicyRegular: appears in the Dock.
                if app.activationPolicy() != 0: continue
                name = app.localizedName()
                if name: out[name] = name
            if out: return out
        except Exception:
            pass
    rc, out = _osa('tell application "System Events" to get name of every application process '
                   'whose background only is false')
    if rc != 0: return {}
    return {n.strip(): n.strip() for n in out.split(",") if n.strip()}


def wait_for_window(proc_name: str, timeout: float = 8) -> bool:
    """Public name for the settle wait, for callers that did not launch the app
    themselves and so never went through open_app."""
    return _wait_for_window(proc_name, timeout)
