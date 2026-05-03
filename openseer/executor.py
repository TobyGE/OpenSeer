"""Action execution via pyautogui.

Logical coordinates assumed (matches ``screen.capture()`` output).
Out-of-bounds coordinates are clamped + warned. ``dry_run=True`` prints
actions without executing — used by default until the user opts in with
``--execute``.
"""
from __future__ import annotations

import shlex
import subprocess
import time
from dataclasses import dataclass

import pyautogui

# Don't fail-safe on tiny mouse moves to corner — agents may legitimately go there
pyautogui.FAILSAFE = True
pyautogui.PAUSE = 0.1


@dataclass
class Action:
    name: str                      # click | double_click | type | key | scroll | wait | open_app | reground | done | fail
    x: int | None = None
    y: int | None = None
    text: str | None = None
    key: str | None = None
    amount: int | None = None
    app: str | None = None         # for open_app
    target: str | None = None      # natural-language element description; resolved to (x,y) by Grounder
    region: list[int] | None = None  # for reground: [x1, y1, x2, y2] crop bbox to "zoom" before grounding
    external: bool = False           # for reground: True ⇒ call the specialist (paid) grounder, not the default
    reason: str | None = None
    thought: str | None = None
    verified_by_steps: list[int] | None = None   # for `done`: prior step indices that ACTUALLY produced this result
    grounding_backend: str | None = None         # which grounder produced (x,y) when from target
    grounding_elapsed_ms: int | None = None


def _clamp(x: int, lo: int, hi: int) -> int:
    return max(lo, min(hi - 1, x))


def execute(action: Action, *, dry_run: bool = True) -> str:
    """Execute one action. Returns a short result/error string for the next-turn prompt."""
    w, h = pyautogui.size()
    name = action.name

    if name in ("done", "fail"):
        return f"task ended: {name} — {action.reason or ''}"

    if name == "wait":
        s = max(0.1, min(5, action.amount or 1))
        if not dry_run:
            time.sleep(s)
        return f"waited {s}s"

    if name in ("click", "double_click", "scroll"):
        if action.x is None or action.y is None:
            return "ERROR: missing x,y"
        x = _clamp(int(action.x), 0, w)
        y = _clamp(int(action.y), 0, h)
        clamped = (x, y) != (int(action.x), int(action.y))
        if name == "click":
            if not dry_run:
                pyautogui.click(x, y)
            return f"clicked ({x},{y})" + (" [clamped]" if clamped else "")
        if name == "double_click":
            if not dry_run:
                pyautogui.doubleClick(x, y)
            return f"double-clicked ({x},{y})" + (" [clamped]" if clamped else "")
        if name == "scroll":
            amt = int(action.amount or 0)
            if not dry_run:
                pyautogui.moveTo(x, y)
                pyautogui.scroll(-amt)  # pyautogui: negative=down on macOS
            return f"scrolled at ({x},{y}) by {amt}"

    if name == "open_app":
        app = (action.app or action.text or "").strip()
        if not app:
            return "ERROR: open_app needs `app` (the application name)"
        if not dry_run:
            r = subprocess.run(["open", "-a", app],
                               capture_output=True, text=True, timeout=8)
            if r.returncode != 0:
                stderr = (r.stderr or "").strip()
                return f"open -a {app!r} failed (rc={r.returncode}): {stderr[:200]}"
        return f"opened app {app!r}"

    if name == "type":
        if not action.text:
            return "ERROR: missing text"
        # If x,y given, click target first to establish focus. This is the
        # robust pattern (matches Anthropic/OpenAI computer-use APIs) — the
        # model declares "type X into the field at (x,y)" and the executor
        # owns the click-then-type sequencing, so the model can't forget
        # focus or have its click+type interleaved across turns.
        clicked_first = False
        if action.x is not None and action.y is not None:
            x = _clamp(int(action.x), 0, w)
            y = _clamp(int(action.y), 0, h)
            if not dry_run:
                pyautogui.click(x, y)
                time.sleep(0.15)  # let focus settle
            clicked_first = True
        if not dry_run:
            pyautogui.typewrite(action.text, interval=0.02)
        prefix = f"clicked ({action.x},{action.y}) → " if clicked_first else ""
        return f"{prefix}typed {action.text!r}"

    if name == "key":
        combo = action.key or ""
        if not combo:
            return "ERROR: missing key"
        # accept "cmd+space", "ctrl+a", "enter", "tab", ...
        keys = [k.strip().lower() for k in combo.split("+")]
        # normalise mac modifiers
        norm = {"cmd": "command", "opt": "option", "ctrl": "ctrl"}
        keys = [norm.get(k, k) for k in keys]
        if not dry_run:
            if len(keys) == 1:
                pyautogui.press(keys[0])
            else:
                pyautogui.hotkey(*keys)
        return f"pressed {'+'.join(keys)}"

    return f"ERROR: unknown action {name}"
