"""One place that decides where the agent's windows go.

app_open parks the app it launches, and the screenshot tool re-parks the
terminal before every capture. Neither of them sees an app that arrived some
other way -- `firefox &` from the shell tool, a browser an MCP server starts
for itself because that is the only way that server can be driven. Those
windows open wherever the app last felt like being, which is usually on top of
the terminal the agent reads its own output from, and nothing ever moved them.

So the rule lives here rather than inside the one tool that used to own it: an
app that was not running when the agent started looking is an app the agent
caused, and it belongs in the same right-hand slice as everything else it
drives. Every tool that can cause a launch reports in afterwards; the cost when
nothing launched is a single list of running apps.

State is module level and therefore per worker process, which is the same
lifetime as the conversation -- exactly the window over which "new" means
"the agent opened it".
"""
import importlib.util, platform, threading
from pathlib import Path

OS = platform.system()

# The two slices. The terminal keeps the left third because it is read, not
# driven; whatever is being driven gets the rest.
SELF_REGION = (0.0, 0.3)
APP_REGION = (0.3, 1.0)

_LOCK = threading.Lock()
_BACKEND = None
_seen: dict = {}            # app -> handle, everything known to have been running
_seeded = False             # whether _seen means anything yet
_parked: dict = {}          # app -> handle, everything this module put in APP_REGION


def backend():
    """The platform window module, loaded once for the whole process.

    Loaded by path rather than imported, because tools/ is not a package the
    tool folders import from each other -- and loading it once is not a detail:
    the module holds the Accessibility handles and the record of where each
    window was last verified to be parked. A second copy would mean a second
    set of empty caches, so every re-park would pay full price.
    """
    global _BACKEND
    if _BACKEND is None:
        name = {"Darwin": "_open_macos", "Windows": "_open_windows"}.get(OS, "_open_linux")
        path = Path(__file__).parent / "app_open" / f"{name}.py"
        spec = importlib.util.spec_from_file_location(f"tools.app_open.{name}", path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        _BACKEND = mod
    return _BACKEND


def _apps() -> dict:
    """{app name: handle snap_region accepts}, or {} if the platform cannot say.

    Nothing here is allowed to raise. Parking is cosmetic and every caller is in
    the middle of doing something that is not: a window manager that cannot
    answer must not fail a shell command or lose a screenshot.
    """
    try:
        return backend().gui_apps() or {}
    except Exception:                                           # noqa: BLE001
        return {}


def _snap(handle, region, focus: bool) -> bool:
    try:
        return bool(backend().snap_region(handle, region[0], region[1], focus=focus))
    except Exception:                                           # noqa: BLE001
        return False


def _wait_for_window(handle) -> bool:
    """Give a just-launched app time to have a window worth moving."""
    try:
        return bool(backend().wait_for_window(handle))
    except Exception:                                           # noqa: BLE001
        return True


def baseline() -> dict:
    """Record what is running now, so anything later is the agent's doing.

    Called before a command that might launch something. Also called implicitly
    by the first park_new, which is why a session that never takes a baseline
    still does not sweep up the windows the user already had open.
    """
    global _seeded
    apps = _apps()
    with _LOCK:
        if apps:
            _seen.update(apps)
            _seeded = True
    return apps


def park_new(self_name: str | None = None, focus: bool = True) -> list:
    """Park every app that has appeared since the last look. Returns their names.

    focus defaults to True because the caller is reporting a launch, and an app
    the agent just opened is the app it is about to drive -- the same reason
    app_open raises the window it opened.
    """
    global _seeded
    apps = _apps()
    if not apps:
        return []
    with _LOCK:
        if not _seeded:
            # First look of the session. Everything up right now predates the
            # agent, so it is remembered, not moved.
            _seen.update(apps)
            _seeded = True
            return []
        fresh = [n for n in apps if n not in _seen and n != self_name]
        _seen.update(apps)

    parked = []
    for name in fresh:
        handle = apps[name]
        # A name in the app list is a process, not yet a window: apps register
        # with the window server well before they have drawn anything, and a
        # snap that lands in that gap moves nothing and reports failure.
        if not _wait_for_window(handle):
            continue
        if _snap(handle, APP_REGION, focus):
            parked.append(name)
            with _LOCK:
                _parked[name] = handle
    return parked


def park_known(self_name: str | None = None) -> list:
    """Re-park what has already been parked once, without raising anything.

    Apps drift: they restore a saved frame, open a second window, or resize
    themselves after the page they loaded settles. Nothing re-checks on its own,
    so this runs on the screenshot path where a drifted window is about to be
    photographed sitting over the terminal. An app still parked costs one
    geometry read -- snap_region short-circuits on the frame it last verified.
    """
    with _LOCK:
        known = list(_parked.items())
    still = []
    for name, handle in known:
        if name == self_name:
            continue
        if _snap(handle, APP_REGION, False):
            still.append(name)
    return still


def park_self(self_name: str | None) -> bool:
    """The agent's own terminal, in the left strip, never raised."""
    if not self_name:
        return False
    return _snap(self_name, SELF_REGION, False)


def forget(name: str) -> None:
    with _LOCK:
        _parked.pop(name, None)


def remember(handle, name: str | None = None) -> None:
    """Record a window somebody else parked, so it is re-checked with the rest.

    app_open does its own snap -- it is the only caller that wants the window
    raised -- and without this the app it opened would be the one app nothing
    ever looked at again.
    """
    with _LOCK:
        _parked[name or (handle if isinstance(handle, str) else str(handle))] = handle
