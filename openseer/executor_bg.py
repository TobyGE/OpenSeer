"""Background input delivery — synthetic CGEvents posted to a specific
process instead of the global HID tap, so the user's frontmost app and
cursor are NOT disturbed.

Ported from Peekaboo's `BackgroundInputDriver.swift` (Apache 2.0,
github.com/openclaw/Peekaboo, commit 73b6239f). Three pieces:

  1. CGEventCreateMouseEvent at the target point — same primitive
     pyautogui uses internally.

  2. Stamp routing fields on the event so the target app's event
     dispatcher accepts it without focus:
       - eventTargetUnixProcessID (the target pid)
       - windowID
       - mouseEventWindowUnderMousePointer (same windowID)
       - mouseEventWindowUnderMousePointerThatCanHandleThisEvent (same)
     Without these, most macOS apps drop synthetic events that
     didn't come from a real focused click.

  3. Post via CGEventPostToPid(pid, event) — public API, routes the
     event to the target process's queue. The global cursor doesn't
     move; the user's mouse position is preserved.

Caveats:
  - Some apps (Catalyst ports, sandboxed apps with strict event
    validation) still reject background events. Test target-by-
    target. Peekaboo's prod path also tries the private SkyLight
    SLEventPostToPid via dlopen as a fallback — we can add that
    later if reliability becomes a problem in the field.
  - Keyboard events (type / hotkey) are NOT covered here; keyboard
    focus routing is harder. For typing, use the foreground path
    (or AX `AXUIElementSetAttributeValue` on the text element).
"""
from __future__ import annotations

import os
import time
from typing import Optional

try:
    from Quartz import (
        CGEventCreateMouseEvent,
        CGEventPostToPid,
        CGEventSetIntegerValueField,
        CGEventSourceCreate,
        CGWindowListCopyWindowInfo,
        kCGEventLeftMouseDown,
        kCGEventLeftMouseUp,
        kCGEventOtherMouseDown,
        kCGEventOtherMouseUp,
        kCGEventRightMouseDown,
        kCGEventRightMouseUp,
        kCGEventSourceStateHIDSystemState,
        kCGMouseButtonCenter,
        kCGMouseButtonLeft,
        kCGMouseButtonRight,
        kCGNullWindowID,
        kCGWindowListExcludeDesktopElements,
        kCGWindowListOptionOnScreenOnly,
    )
    _AVAILABLE = True
except ImportError:
    _AVAILABLE = False


# CGEventField numeric IDs. PyObjC's Quartz module exposes most as
# module-level constants — we use those when present, otherwise fall
# back to the values documented in CGEventTypes.h. The fallbacks
# below match the current macOS SDK (verified on macOS 14+):
#
#   kCGMouseEventClickState                                  = 1
#   kCGEventTargetUnixProcessID                              = 40
#   kCGMouseEventWindowUnderMousePointer                     = 91
#   kCGMouseEventWindowUnderMousePointerThatCanHandleThisEvent = 92
#
# (codex review on f01f3b4 caught earlier guesses of 39/35/36 which
# were either wrong constants or stale values from older SDKs.)
#
# `kCGWindowIDEventField` isn't a documented CGEventField; Peekaboo
# stamps it via Swift's `.windowID` enum case which compiles to 51
# in current SDKs. We pass 51 here as a best-effort — if the slot
# isn't honored the other two windowUnder* fields still carry the
# routing info the target dispatcher needs.
def _field(name: str, fallback: int) -> int:
    try:
        import Quartz as _q
        val = getattr(_q, name, None)
        if val is not None:
            return int(val)
    except Exception:
        pass
    return fallback


_FIELD_CLICK_STATE         = _field("kCGMouseEventClickState",                                        1)
_FIELD_WINDOW_UNDER_PTR    = _field("kCGMouseEventWindowUnderMousePointer",                           91)
_FIELD_WINDOW_UNDER_PTR_OK = _field("kCGMouseEventWindowUnderMousePointerThatCanHandleThisEvent",     92)
_FIELD_TARGET_PID          = _field("kCGEventTargetUnixProcessID",                                    40)
_FIELD_WINDOW_ID           = _field("kCGWindowIDEventField",                                          51)


# Dictionary keys for CGWindowListCopyWindowInfo. PyObjC returns NSDictionary
# entries keyed by NSString — accessible from Python as literal strings.
_WIN_OWNER_PID = "kCGWindowOwnerPID"
_WIN_LAYER     = "kCGWindowLayer"
_WIN_BOUNDS    = "kCGWindowBounds"
_WIN_NUMBER    = "kCGWindowNumber"


_BUTTON_MAP = None
if _AVAILABLE:
    _BUTTON_MAP = {
        "left":   (kCGEventLeftMouseDown,  kCGEventLeftMouseUp,  kCGMouseButtonLeft),
        "right":  (kCGEventRightMouseDown, kCGEventRightMouseUp, kCGMouseButtonRight),
        "middle": (kCGEventOtherMouseDown, kCGEventOtherMouseUp, kCGMouseButtonCenter),
    }


def available() -> bool:
    """True iff the platform / PyObjC bundle supports background input."""
    return _AVAILABLE


def click_background(
    x: int,
    y: int,
    pid: int,
    *,
    button: str = "left",
    count: int = 1,
) -> Optional[str]:
    """Synthetic click at (x, y) delivered ONLY to the target pid's
    event queue. The user's hardware cursor is NOT moved.

    Returns None on success, or a short error string on failure.
    """
    if not _AVAILABLE:
        return "Quartz not available (need pyobjc-framework-Quartz)"
    if button not in (_BUTTON_MAP or {}):
        return f"unknown button: {button}"
    if pid <= 0:
        return f"invalid target pid: {pid}"
    if not _is_process_alive(pid):
        return f"target pid {pid} is not running"

    down_type, up_type, cg_button = _BUTTON_MAP[button]
    source = CGEventSourceCreate(kCGEventSourceStateHIDSystemState)
    count_clamped = max(1, min(3, int(count)))
    point = (float(x), float(y))
    win_id = _window_id_at(x, y, pid)

    for click_index in range(1, count_clamped + 1):
        down = CGEventCreateMouseEvent(source, down_type, point, cg_button)
        up   = CGEventCreateMouseEvent(source, up_type,   point, cg_button)
        if down is None or up is None:
            return "failed to create CGEvent"

        for ev in (down, up):
            CGEventSetIntegerValueField(ev, _FIELD_CLICK_STATE, click_index)
            CGEventSetIntegerValueField(ev, _FIELD_TARGET_PID,  pid)
            if win_id is not None:
                CGEventSetIntegerValueField(ev, _FIELD_WINDOW_ID,           win_id)
                CGEventSetIntegerValueField(ev, _FIELD_WINDOW_UNDER_PTR,    win_id)
                CGEventSetIntegerValueField(ev, _FIELD_WINDOW_UNDER_PTR_OK, win_id)

        CGEventPostToPid(pid, down)
        time.sleep(0.030)
        CGEventPostToPid(pid, up)

        if click_index < count_clamped:
            time.sleep(0.080)

    return None


def _window_id_at(x: int, y: int, pid: int) -> Optional[int]:
    """Return the CGWindowID owned by `pid` that contains (x, y). Used
    to stamp routing fields so the target app's event dispatcher
    accepts the synthetic event. None if no match — we still try the
    click but the app is less likely to honor it."""
    flags = kCGWindowListOptionOnScreenOnly | kCGWindowListExcludeDesktopElements
    windows = CGWindowListCopyWindowInfo(flags, kCGNullWindowID)
    if not windows:
        return None
    for w in windows:
        if w.get(_WIN_OWNER_PID) != pid:
            continue
        if w.get(_WIN_LAYER, 0) != 0:
            continue
        bounds = w.get(_WIN_BOUNDS)
        if not bounds:
            continue
        bx = bounds.get("X", 0)
        by = bounds.get("Y", 0)
        bw = bounds.get("Width", 0)
        bh = bounds.get("Height", 0)
        if bx <= x < bx + bw and by <= y < by + bh:
            return int(w.get(_WIN_NUMBER, 0)) or None
    return None


def _is_process_alive(pid: int) -> bool:
    """kill(pid, 0) trick: returns 0 if process exists, raises
    ProcessLookupError if not, PermissionError if exists-but-not-ours."""
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
