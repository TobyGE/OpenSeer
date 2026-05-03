"""Full-screen capture for the agent loop.

Uses CGWindowListCreateImage (Quartz) instead of the ``screencapture``
shell binary. Reason: ``screencapture`` silently skips system overlay
windows like Spotlight, completion popups, and the menu-bar dropdowns,
so the agent can't see what it just opened. CGWindowListCreateImage
includes those overlays.

macOS Retina gotcha: ``pyautogui.size()`` returns logical points (eg.
1600×900) while the captured image is in physical pixels (eg. 3200×1800).
We downscale to logical so model coordinates match pyautogui clicks 1:1.
"""
from __future__ import annotations

from dataclasses import dataclass

import pyautogui
from PIL import Image


@dataclass
class Frame:
    image: Image.Image    # logical-resolution PIL image (matches pyautogui)
    logical_size: tuple[int, int]
    physical_size: tuple[int, int]


def _cgimage_to_pil(cg_image) -> Image.Image:
    """Convert a CGImageRef to a PIL.Image via raw pixel buffer."""
    import Quartz
    from Quartz import (
        CGImageGetWidth, CGImageGetHeight, CGImageGetBytesPerRow,
        CGDataProviderCopyData, CGImageGetDataProvider,
    )
    w = CGImageGetWidth(cg_image)
    h = CGImageGetHeight(cg_image)
    stride = CGImageGetBytesPerRow(cg_image)
    data = CGDataProviderCopyData(CGImageGetDataProvider(cg_image))
    buf = bytes(data)
    # CGImage is BGRA (premultiplied) in the layout we get back from CG.
    # PIL.Image.frombuffer with 'BGRA' is supported via raw decoder.
    img = Image.frombuffer("RGBA", (w, h), buf, "raw", "BGRA", stride, 1)
    return img.convert("RGB")


def capture() -> Frame:
    """Grab the full screen including system overlays (Spotlight etc.)."""
    from Quartz import (
        CGWindowListCreateImage,
        CGRectInfinite,
        kCGWindowListOptionOnScreenOnly,
        kCGNullWindowID,
        kCGWindowImageDefault,
    )
    cg = CGWindowListCreateImage(
        CGRectInfinite,
        kCGWindowListOptionOnScreenOnly,
        kCGNullWindowID,
        kCGWindowImageDefault,
    )
    if cg is None:
        raise RuntimeError(
            "CGWindowListCreateImage returned None — Screen Recording "
            "permission missing? System Settings → Privacy & Security → "
            "Screen Recording, add iTerm (and your python binary)."
        )
    physical = _cgimage_to_pil(cg)
    pw, ph = physical.size
    lw, lh = pyautogui.size()
    if (pw, ph) != (lw, lh):
        logical = physical.resize((lw, lh), Image.LANCZOS)
    else:
        logical = physical.copy()
    return Frame(image=logical, logical_size=(lw, lh), physical_size=(pw, ph))
