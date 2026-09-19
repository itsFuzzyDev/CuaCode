from tools import _window


def run(args: dict, ctx) -> dict:
    app = args["app"]
    m = _window.backend()

    # The agent's own window, by name -- the session's own app, not whatever
    # happens to be frontmost right now. Mid-run that is usually the app being
    # driven, and parking IT in the left strip is how the driven app and the
    # agent's window swap places for no reason (and how a GUI frontend's
    # window -- the agent's actual home -- ended up shrunk to a third).
    self_snapped = _window.park_self(getattr(ctx, "self_identity", None))

    # Taken before the launch so that anything the app drags up with it -- a
    # helper process, a second app it hands the request to -- is known to be new
    # rather than assumed to have always been there.
    _window.baseline()

    handle = m.open_app(app)
    if not handle:
        return {"ok": True, "app": app, "snapped": False, "self_snapped": self_snapped,
                "note": "app opened but no window was detected in time; not snapped"}

    # The one snap that raises: the agent is about to drive this app, so it
    # has to end the call holding keyboard focus.
    app_snapped = m.snap_region(handle, *_window.APP_REGION, focus=True)
    if app_snapped:
        _window.remember(handle, app)
    # The app is accounted for now, so the next caller that asks what is new
    # does not find it and park it a second time.
    _window.baseline()
    return {"ok": True, "app": app, "snapped": app_snapped, "self_snapped": self_snapped}
