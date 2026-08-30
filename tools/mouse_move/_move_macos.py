import Quartz as _Q

def move(x: int, y: int):
    _Q.CGEventPost(_Q.kCGHIDEventTap,
                    _Q.CGEventCreateMouseEvent(None, _Q.kCGEventMouseMoved, (x, y), 0))

def cursor() -> tuple[int, int]:
    """Where the pointer actually is, in the same logical points move() takes.

    The same read-back the click tool relies on, and read the same way -- after
    a wait, since the post is asynchronous. See tools/_pointer.py.
    """
    loc = _Q.CGEventGetLocation(_Q.CGEventCreate(None))
    return round(loc.x), round(loc.y)