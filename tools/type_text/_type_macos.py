import Quartz as _Q
import time

_KVK_RETURN = 0x24

def _press_return():
    down = _Q.CGEventCreateKeyboardEvent(None, _KVK_RETURN, True)
    _Q.CGEventPost(_Q.kCGHIDEventTap, down)
    up = _Q.CGEventCreateKeyboardEvent(None, _KVK_RETURN, False)
    _Q.CGEventPost(_Q.kCGHIDEventTap, up)

def type_text(text: str, delay: float = 0.01):
    for ch in text:
        if ch == "\n":
            _press_return()
        else:
            event = _Q.CGEventCreateKeyboardEvent(None, 0, True)
            _Q.CGEventKeyboardSetUnicodeString(event, len(ch), ch)
            _Q.CGEventPost(_Q.kCGHIDEventTap, event)
            event_up = _Q.CGEventCreateKeyboardEvent(None, 0, False)
            _Q.CGEventKeyboardSetUnicodeString(event_up, len(ch), ch)
            _Q.CGEventPost(_Q.kCGHIDEventTap, event_up)
        if delay: time.sleep(delay)