"""
Trackpad pinch-to-zoom for Tk Canvas on macOS.

Tk/Aqua does not expose pinch as <MouseWheel>; we attach an
NSMagnificationGestureRecognizer to the widget's NSView when PyObjC is installed.
"""

from __future__ import annotations

import sys
from typing import Any, Callable

try:
    import objc
    from AppKit import NSMagnificationGestureRecognizer, NSView
    from Foundation import NSObject

    class _PinchTarget(NSObject):
        def initWithCallback_(self, callback):  # noqa: N802
            self = objc.super(_PinchTarget, self).init()
            if self is None:
                return None
            self._py_callback = callback
            return self

        def magnify_(self, recognizer):  # noqa: N802
            try:
                m = float(recognizer.magnification())
                cb = self._py_callback
                if cb is not None:
                    cb(m)
            finally:
                try:
                    recognizer.setMagnification_(0.0)
                except Exception:
                    pass

    _OBJC_PINCH_AVAILABLE = True
except ImportError:
    _OBJC_PINCH_AVAILABLE = False
    objc = None  # type: ignore[misc, assignment]
    NSMagnificationGestureRecognizer = None  # type: ignore[misc, assignment]
    NSView = None  # type: ignore[misc, assignment]
    NSObject = None  # type: ignore[misc, assignment]
    _PinchTarget = None  # type: ignore[misc, assignment]


def attach_canvas_pinch_zoom(
    canvas: Any,
    on_magnification_delta: Callable[[float], None],
) -> list[Any] | None:
    """
    Call on_magnification_delta(m) with Cocoa magnification delta (small floats, often ±0.02).
    Returns a list of ObjC objects to keep alive, or None if not attached.
    """
    if sys.platform != "darwin" or not _OBJC_PINCH_AVAILABLE:
        return None

    try:
        wid = int(canvas.winfo_id())
    except (TypeError, ValueError):
        return None
    if wid == 0:
        return None

    try:
        view = objc.ObjCInstance(wid)
    except Exception:
        return None
    if view is None:
        return None
    try:
        if not view.isKindOfClass_(NSView):
            return None
    except Exception:
        return None

    keepalive: list[Any] = []

    try:
        target = _PinchTarget.alloc().initWithCallback_(on_magnification_delta)
        if target is None:
            return None
        gr = NSMagnificationGestureRecognizer.alloc().initWithTarget_action_(target, b"magnify:")
        if gr is None:
            return None
        view.addGestureRecognizer_(gr)
        keepalive.extend([target, gr])
        return keepalive
    except Exception:
        return None
