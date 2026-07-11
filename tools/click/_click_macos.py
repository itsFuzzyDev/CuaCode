import Quartz as _Q

_BUTTONS = {
    "left": (_Q.kCGEventLeftMouseDown, _Q.kCGEventLeftMouseUp, _Q.kCGMouseButtonLeft),
    "right": (_Q.kCGEventRightMouseDown, _Q.kCGEventRightMouseUp, _Q.kCGMouseButtonRight),
    "middle": (_Q.kCGEventOtherMouseDown, _Q.kCGEventOtherMouseUp, _Q.kCGMouseButtonCenter),
}

def click(x: int, y: int, button: str = "left", clicks: int = 1):
    down_type, up_type, btn = _BUTTONS[button]
    for i in range(clicks):
        down = _Q.CGEventCreateMouseEvent(None, down_type, (x, y), btn)
        _Q.CGEventSetIntegerValueField(down, _Q.kCGMouseEventClickState, i + 1)
        _Q.CGEventPost(_Q.kCGHIDEventTap, down)
        up = _Q.CGEventCreateMouseEvent(None, up_type, (x, y), btn)
        _Q.CGEventSetIntegerValueField(up, _Q.kCGMouseEventClickState, i + 1)
        _Q.CGEventPost(_Q.kCGHIDEventTap, up)