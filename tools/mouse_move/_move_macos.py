import Quartz as _Q

def move(x: int, y: int):
    _Q.CGEventPost(_Q.kCGHIDEventTap,
                    _Q.CGEventCreateMouseEvent(None, _Q.kCGEventMouseMoved, (x, y), 0))