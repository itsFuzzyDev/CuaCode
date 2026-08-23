from tools import _window


def run(args: dict, ctx) -> dict:
    app = args["app"]
    m = _window.backend()

    self_handle = m.get_frontmost()
    self_snapped = _window.park_self(self_handle)

    # Taken before the launch so that anything the app drags up with it -- a
    # helper process, a second app it hands the request to -- is known to be new
    # rather than assumed to have always been there.
    _window.baseline()

    handle = m.open_app(app)
    if not handle:
        return {"ok": True, "app": app, "snapped": False, "self_snapped": self_snapped,
                "note": "app opened but window not detected within 5s"}

    # The one snap that raises: the agent is about to drive this app, so it
    # has to end the call holding keyboard focus.
    app_snapped = m.snap_region(handle, *_window.APP_REGION, focus=True)
    if app_snapped:
        _window.remember(handle, app)
    # The app is accounted for now, so the next caller that asks what is new
    # does not find it and park it a second time.
    _window.baseline()
    return {"ok": True, "app": app, "snapped": app_snapped, "self_snapped": self_snapped}
