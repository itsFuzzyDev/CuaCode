import Quartz as _Q

def move(x: int, y: int):
    _Q.CGEventPost(_Q.kCGHIDEventTap,
                    _Q.CGEventCreateMouseEvent(None, _Q.kCGEventMouseMoved, (x, y), 0))

def cursor() -> tuple[int, int]:
    """Where the pointer actually is, in the same logical points move() takes.

    Worth reading back: CGEventPost does not fail when the process is missing
    Accessibility permission, it is simply dropped. A no-op looks exactly like
    a click in the wrong place from the agent's side, and it will keep
    re-aiming at a target it was never able to reach.
    """
    loc = _Q.CGEventGetLocation(_Q.CGEventCreate(None))
    return round(loc.x), round(loc.y)