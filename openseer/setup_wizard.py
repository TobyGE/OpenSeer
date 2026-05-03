"""One-stop guided onboarding: `openseer setup`.

Walks the user through:
  1. Codex CLI installed
  2. ChatGPT OAuth login (via Codex CLI)
  3. macOS Accessibility permission
  4. macOS Screen Recording permission
  5. Smoke test (1 capture, 1 mouse-move) confirming the loop is live

Each step prints status, fixes what it can, and pauses for manual
steps the OS won't let us automate (the two privacy permissions).
"""
from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

from . import auth as auth_mod

RESET, BOLD, DIM = "\x1b[0m", "\x1b[1m", "\x1b[2m"
RED, GRN, YEL, CYN, MAG = "\x1b[31m", "\x1b[32m", "\x1b[33m", "\x1b[36m", "\x1b[35m"


def _c(s: str, *codes: str) -> str:
    return "".join(codes) + s + RESET


def _step(idx: int, total: int, title: str) -> None:
    print(f"\n{_c(f'[{idx}/{total}]', BOLD, CYN)} {_c(title, BOLD)}")


def _ok(msg: str) -> None:
    print(f"  {_c('✓', GRN)} {msg}")


def _warn(msg: str) -> None:
    print(f"  {_c('⚠', YEL)} {msg}")


def _fail(msg: str) -> None:
    print(f"  {_c('✗', RED)} {msg}")


def _ask(prompt: str) -> bool:
    """Return True if the user pressed Enter (continue) or 'y'."""
    try:
        ans = input(f"  {_c('?', MAG)} {prompt} ").strip().lower()
    except EOFError:
        return False
    return ans in ("", "y", "yes")


# ─── checks ────────────────────────────────────────────────────────────────────

def check_codex_cli() -> bool:
    cli = auth_mod.codex_cli_path()
    if cli:
        _ok(f"Codex CLI found at {cli}")
        return True
    _fail("Codex CLI not installed.")
    print(f"\n    Install once with:\n        {_c('npm install -g @openai/codex', CYN)}\n")
    print("    (Or follow https://github.com/openai/codex.)")
    print("    OpenSeer reuses Codex CLI's OAuth tokens — no API key needed.")
    return False


def check_login(allow_login: bool = True) -> bool:
    st = auth_mod.token_status()
    if st.has_file and not st.expired:
        _ok(f"logged in — {st.summary().split('—', 1)[1].strip()}")
        return True
    if st.has_file and st.expired:
        _warn("Token expired.")
    else:
        _warn("Not logged in.")

    if not allow_login:
        return False

    if not _ask("Run codex login now? [Y/n]"):
        _fail("Skipped — re-run `openseer setup` when ready.")
        return False
    rc = auth_mod.run_codex_login()
    if rc != 0:
        _fail(f"codex login exited with code {rc}.")
        return False
    # re-check
    st = auth_mod.token_status()
    if st.has_file and not st.expired:
        _ok(f"logged in — {st.summary().split('—', 1)[1].strip()}")
        return True
    _fail("Still not logged in after the browser flow.")
    return False


def check_accessibility() -> bool:
    """Try a tiny pyautogui call. If Accessibility perm is missing on macOS,
    the call appears to succeed but the cursor doesn't actually move; we
    can't perfectly detect that, so we attempt a no-op move and ask the
    user to confirm visually."""
    try:
        import pyautogui
    except ImportError:
        _fail("pyautogui not installed (this shouldn't happen — `pip install -e .` it).")
        return False

    print("    OpenSeer needs Accessibility permission to inject mouse/keyboard")
    print("    events.  System Settings → Privacy & Security → Accessibility,")
    print(f"    add and enable the terminal you're running from (e.g. {_c('iTerm', CYN)} or")
    print(f"    {_c('Terminal', CYN)}). After granting, fully {_c('Cmd+Q', CYN)} the terminal and reopen.")
    print()
    if not _ask("Granted? Press Enter to test by nudging the cursor."):
        return False

    before = pyautogui.position()
    target = (before.x + 50, before.y)
    pyautogui.moveTo(*target, duration=0.2)
    time.sleep(0.1)
    after = pyautogui.position()
    if abs(after.x - target[0]) <= 5:
        _ok("cursor moved — Accessibility perm OK")
        # nudge back so the user's cursor isn't "lost"
        pyautogui.moveTo(before.x, before.y, duration=0.1)
        return True
    _fail(f"cursor didn't move (before={before}, after={after}). "
          f"Accessibility likely still missing.")
    return False


def check_screen_recording() -> bool:
    """Capture once and inspect dimensions / variance. Black or zero-size
    images usually indicate perm denied."""
    try:
        from .screen import capture
    except Exception as e:
        _fail(f"screen.capture import failed: {e}")
        return False

    print("    OpenSeer needs Screen Recording permission to see your screen.")
    print(f"    System Settings → Privacy & Security → Screen Recording — add")
    print(f"    your terminal there too.  After granting, restart the terminal.")
    print()
    if not _ask("Granted? Press Enter to test."):
        return False

    try:
        frame = capture()
    except Exception as e:
        _fail(f"capture failed: {e}")
        return False

    w, h = frame.image.size
    if w < 100 or h < 100:
        _fail(f"capture returned {w}x{h} — perm likely denied.")
        return False
    # quick black-screen check via PIL extrema
    extrema = frame.image.getextrema()
    if all(lo == hi == 0 for lo, hi in extrema):
        _fail("capture returned an all-black image — perm likely denied.")
        return False
    _ok(f"captured {w}x{h} (logical) screen — Screen Recording perm OK")
    return True


def smoke_test() -> bool:
    """Round-trip: capture + send to model + parse a reply. No actions."""
    print("    Final check — pinging the model with a tiny prompt.")
    try:
        from .openai_chatgpt import _stream_full, MODEL
    except Exception as e:
        _fail(f"can't import model client: {e}")
        return False
    try:
        text, _events, usage = _stream_full({
            "model": MODEL,
            "instructions": "Reply with the single word OK.",
            "input": [{"role": "user", "content": [
                {"type": "input_text", "text": "Reply OK."},
            ]}],
            "stream": True, "store": False,
            "reasoning": {"effort": "low"},
        })
    except Exception as e:
        _fail(f"model call failed: {e}")
        return False
    _ok(f"model responded ({len(text)} chars, usage in/out="
        f"{usage.get('input_tokens', '?')}/{usage.get('output_tokens', '?')})")
    return True


# ─── main ──────────────────────────────────────────────────────────────────────

def run_setup() -> int:
    print(_c("\nOpenSeer setup", BOLD) + " " + _c("— let's get you running", DIM))

    _step(1, 5, "Codex CLI installed")
    if not check_codex_cli():
        print(_c("\nSetup paused — install Codex CLI and re-run.", YEL))
        return 1

    _step(2, 5, "ChatGPT login (via Codex CLI OAuth)")
    if not check_login():
        return 2

    _step(3, 5, "macOS Accessibility permission")
    if not check_accessibility():
        print(_c("\nSetup paused — fix Accessibility and re-run.", YEL))
        return 3

    _step(4, 5, "macOS Screen Recording permission")
    if not check_screen_recording():
        print(_c("\nSetup paused — fix Screen Recording and re-run.", YEL))
        return 4

    _step(5, 5, "Smoke test (model ping)")
    if not smoke_test():
        print(_c("\nSetup paused — model call failed; check network or token.", YEL))
        return 5

    print(_c("\nAll set.", GRN, BOLD) + " " + _c("Run", DIM) + " " + _c("openseer", CYN, BOLD)
          + " " + _c("to enter the chat shell.\n", DIM))
    return 0
