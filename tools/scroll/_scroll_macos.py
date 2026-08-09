import Quartz as _Q

def scroll(x: int, y: int, dx: int, dy: int):
    move = _Q.CGEventCreateMouseEvent(None, _Q.kCGEventMouseMoved, (x, y), 0)
    _Q.CGEventPost(_Q.kCGHIDEventTap, move)
    event = _Q.CGEventCreateScrollWheelEvent(None, _Q.kCGScrollEventUnitPixel, 2, dy, dx)
    _Q.CGEventPost(_Q.kCGHIDEventTap, event)