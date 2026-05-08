"""One-stop guided onboarding: `openseer setup`.

Walks the user through:
  1. Pick model provider (OpenAI gpt-5.5 via Codex OAuth | Anthropic Claude
     Haiku via Claude Code OAuth) — auto-detects what's available, lets
     the user pick if both, persists choice to ~/.openseer/config.json
  2. Verify chosen provider's auth
  3. macOS Accessibility permission
  4. macOS Screen Recording permission
  5. Smoke test confirming the loop is live

Each step prints status, fixes what it can, and pauses for manual
steps the OS won't let us automate (the two privacy permissions).
"""
from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

from . import auth as auth_mod


_CONFIG_PATH = Path.home() / ".openseer" / "config.json"


def _load_config() -> dict:
    if not _CONFIG_PATH.exists():
        return {}
    try:
        return json.loads(_CONFIG_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_config(cfg: dict) -> None:
    _CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    _CONFIG_PATH.write_text(json.dumps(cfg, indent=2), encoding="utf-8")

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


# ─── provider detection + selection ──────────────────────────────────────────

def _detect_openai() -> tuple[bool, str]:
    """Returns (usable, summary). Usable means: Codex CLI installed AND
    a non-expired auth token present."""
    cli = auth_mod.codex_cli_path()
    if not cli:
        return False, "Codex CLI not installed"
    st = auth_mod.token_status()
    if st.error:
        return False, f"auth file unreadable: {st.error}"
    if not st.has_file:
        return False, "not logged in"
    if st.expired:
        return False, "token expired"
    return True, st.summary()


def _detect_anthropic() -> tuple[bool, str]:
    """Returns (usable, summary). Usable means: Claude Code OAuth blob
    is in macOS Keychain with a non-expired access token."""
    try:
        from . import anthropic_messages as a
    except Exception as e:
        return False, f"module import failed: {e}"
    st = a.token_status()
    if not st.get("present"):
        return False, st.get("error") or "not signed in via Claude Code"
    expires_in = int(st.get("expires_in_s") or 0)
    if expires_in < 0:
        return False, f"token expired ({-expires_in}s ago)"
    sub = st.get("subscription") or "?"
    return True, f"Claude {sub} sub, expires in {expires_in // 60}min"


def _claude_cli_path() -> str | None:
    """Return path to the `claude` CLI if installed."""
    import shutil
    return shutil.which("claude")


def run_claude_login() -> int:
    """Trigger Claude Code's OAuth browser flow via `claude auth login`.
    Returns the subprocess exit code; 0 = success, the OAuth blob is now
    in macOS Keychain under service 'Claude Code-credentials'."""
    cli = _claude_cli_path()
    if not cli:
        _fail("`claude` CLI not found — install via `npm install -g "
              "@anthropic-ai/claude-code`, or sign in via Claude.app.")
        return 127
    print(f"    Launching {_c('claude auth login', CYN)} — your browser will "
          f"open for OAuth.")
    try:
        rc = subprocess.run([cli, "auth", "login"]).returncode
    except KeyboardInterrupt:
        _warn("Login cancelled.")
        return 130
    return rc


def choose_provider() -> str | None:
    """Detect both providers, prompt the user to pick (offering an
    OAuth login flow if the chosen one isn't authenticated), persist
    the choice to config, and return it. Returns None on abort."""
    cfg = _load_config()
    saved = (cfg.get("provider") or "").strip().lower()
    oai_ok, oai_msg = _detect_openai()
    ant_ok, ant_msg = _detect_anthropic()

    def _summary(ok: bool, msg: str) -> str:
        mark = _c('✓', GRN) if ok else _c('✗', RED)
        return f"{mark} {msg}"

    print(f"  {_c('1) OpenAI gpt-5.5', BOLD)}        ({_summary(oai_ok, oai_msg)})")
    print(f"  {_c('2) Anthropic Haiku 4.5', BOLD)}   ({_summary(ant_ok, ant_msg)})")

    # Always let the user pick — even if one is unauth'd, we'll offer
    # to launch the login flow. Default: their saved choice if any,
    # else whichever is already auth'd, else anthropic (faster / cheaper).
    if saved in ("openai", "anthropic"):
        default = saved
    elif ant_ok and not oai_ok:
        default = "anthropic"
    elif oai_ok and not ant_ok:
        default = "openai"
    else:
        default = "anthropic"
    try:
        ans = input(
            f"  {_c('?', MAG)} Which to use? "
            f"[a]nthropic / [o]penai (default: {default}): "
        ).strip().lower()
    except EOFError:
        ans = ""
    if ans.startswith("a"):
        choice = "anthropic"
    elif ans.startswith("o"):
        choice = "openai"
    else:
        choice = default

    # If the chosen provider isn't authenticated, offer to launch
    # its OAuth flow right now.
    if choice == "anthropic" and not ant_ok:
        _warn(f"Claude OAuth not present: {ant_msg}")
        if _ask("Launch `claude auth login` now to sign in via browser? [Y/n]"):
            rc = run_claude_login()
            if rc == 0:
                ant_ok2, ant_msg2 = _detect_anthropic()
                if ant_ok2:
                    _ok(ant_msg2)
                else:
                    _fail(f"Login finished but token still missing: {ant_msg2}")
                    return None
            else:
                _fail(f"`claude auth login` exited with code {rc}.")
                return None
        else:
            _fail("Skipped — re-run `openseer setup` after signing in.")
            return None
    elif choice == "openai" and not oai_ok:
        _warn(f"Codex OAuth not present: {oai_msg}")
        # Re-use existing helper (offers `codex login`).
        if not check_login():
            return None

    cfg["provider"] = choice
    _save_config(cfg)
    _ok(f"Saved provider={choice} to {_CONFIG_PATH}")
    return choice


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
    if st.error:
        _fail(f"auth file is unreadable: {st.error}")
        if not allow_login:
            return False
        if not _ask("Re-run codex login to recreate it? [Y/n]"):
            return False
        rc = auth_mod.run_codex_login()
        if rc != 0:
            _fail(f"codex login exited with code {rc}.")
            return False
        st = auth_mod.token_status()
    if st.has_file and not st.expired and not st.error:
        _ok(st.summary())
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
    if st.has_file and not st.expired and not st.error:
        _ok(st.summary())
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
    # Pick a target ≥ 50 px away that's guaranteed inside the screen.
    # If the cursor is near the right edge we go LEFT instead.
    sw, sh = pyautogui.size()
    if before.x + 50 < sw - 10:
        target = (before.x + 50, before.y)
    else:
        target = (max(10, before.x - 50), before.y)
    pyautogui.moveTo(*target, duration=0.2)
    time.sleep(0.1)
    after = pyautogui.position()
    if abs(after.x - target[0]) <= 5 and abs(after.y - target[1]) <= 5:
        _ok("cursor moved — Accessibility perm OK")
        # nudge back so the user's cursor isn't "lost"
        pyautogui.moveTo(before.x, before.y, duration=0.1)
        return True
    _fail(f"cursor didn't move (before={before}, target={target}, after={after}). "
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


def smoke_test(provider: str) -> bool:
    """Round-trip: capture + send to model + parse a reply. No actions."""
    print(f"    Final check — pinging the {provider} model with a tiny prompt.")
    try:
        if provider == "anthropic":
            from .anthropic_messages import stream_full as _stream, MODEL
        else:
            from .openai_chatgpt import _stream_full as _stream, MODEL
    except Exception as e:
        _fail(f"can't import model client: {e}")
        return False
    try:
        text, _events, usage = _stream({
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
    _ok(f"{MODEL} responded ({len(text)} chars, usage in/out="
        f"{usage.get('input_tokens', '?')}/{usage.get('output_tokens', '?')})")
    return True


# ─── main ──────────────────────────────────────────────────────────────────────

def configure_telegram() -> bool:
    """Optional: configure Telegram inbound channel so the user can
    drive OpenSeer from their phone. Skipped if user declines."""
    cfg = _load_config()
    existing = cfg.get("telegram") or {}
    has_token = bool((existing.get("token") or "").strip())

    if has_token and not _ask("Telegram already configured. Re-configure? [y/N]"):
        _ok("Keeping existing Telegram config.")
        return True

    print("    OpenSeer can listen on a Telegram bot so you can issue tasks")
    print("    from your phone. Setup takes ~2 minutes:")
    print(f"      1. open Telegram, message {_c('@BotFather', CYN)}")
    print(f"      2. send {_c('/newbot', CYN)}, follow prompts, copy the token it gives you")
    print(f"      3. find your bot in Telegram, send it any message,")
    print(f"         the daemon will print your chat_id on first message receive")
    print()

    if not _ask("Set up Telegram now? [y/N]"):
        _warn("Skipped — re-run `openseer setup` later, or edit ~/.openseer/config.json directly.")
        return True            # not a hard failure

    try:
        token = input(f"  {_c('?', MAG)} Bot token: ").strip()
    except EOFError:
        token = ""
    if not token:
        _fail("No token entered. Skipping.")
        return True
    if ":" not in token or len(token) < 30:
        _warn(f"Token doesn't look right (expect 'NUMBER:LETTERS', got {token[:10]}…). "
              "Saving anyway — daemon will validate on start.")

    # Validate token live so the user gets immediate feedback.
    print("    verifying token …")
    try:
        from .inbox.telegram import TelegramBot, TelegramError
        probe = TelegramBot(token)
        me = probe.get_me()
        _ok(f"bot @{me.get('username')} ({me.get('first_name')}) reachable")
    except TelegramError as e:
        _fail(f"token rejected: {e}")
        return False
    except Exception as e:
        _fail(f"could not reach Telegram API: {e}")
        return False

    # Optional allowed_chat_ids
    print(f"  {_c('?', MAG)} Allowed chat_ids (comma-separated; leave blank to fill in later):")
    try:
        raw_ids = input("    > ").strip()
    except EOFError:
        raw_ids = ""
    allowed: list[int] = []
    if raw_ids:
        for tok in raw_ids.replace(",", " ").split():
            try:
                allowed.append(int(tok))
            except ValueError:
                _warn(f"ignoring non-numeric chat_id: {tok!r}")
    if not allowed:
        _warn("No allowed_chat_ids set — daemon will print incoming chat_ids "
              "to the console on first run; copy yours into config.json afterward.")

    try:
        prefix = input(f"  {_c('?', MAG)} Trigger prefix (blank = treat all messages as tasks): ").strip()
    except EOFError:
        prefix = ""

    cfg["telegram"] = {
        "enabled": True,
        "token": token,
        "allowed_chat_ids": allowed,
        "trigger_prefix": prefix,
        "max_steps": int(existing.get("max_steps") or 25),
        "confirm_each": bool(existing.get("confirm_each", False)),
    }
    _save_config(cfg)
    _ok(f"Telegram saved to {_CONFIG_PATH}")
    print(f"    Start the daemon with: {_c('openseer daemon', CYN)}")
    return True


def run_setup() -> int:
    print(_c("\nOpenSeer setup", BOLD) + " " + _c("— let's get you running", DIM))

    _step(1, 6, "Choose model provider")
    provider = choose_provider()
    if provider is None:
        print(_c("\nSetup paused — no usable provider. Install Codex CLI "
                 "and run `codex login`, OR open Claude desktop and sign "
                 "in, then re-run setup.", YEL))
        print(f"    Codex:  {_c('npm install -g @openai/codex', CYN)}")
        print(f"    Claude: open Claude.app or run `claude` CLI to sign in")
        return 1

    _step(2, 6, "macOS Accessibility permission")
    if not check_accessibility():
        print(_c("\nSetup paused — fix Accessibility and re-run.", YEL))
        return 2

    _step(3, 6, "macOS Screen Recording permission")
    if not check_screen_recording():
        print(_c("\nSetup paused — fix Screen Recording and re-run.", YEL))
        return 3

    _step(4, 6, f"Smoke test ({provider} model ping)")
    if not smoke_test(provider):
        print(_c("\nSetup paused — model call failed; check network or token.",
                 YEL))
        return 4

    _step(5, 6, "Personality + memory files (SOUL.md / MEMORY.md)")
    from .personal import (
        seed_defaults_if_missing, SOUL_PATH, MEMORY_PATH,
    )
    soul_new, mem_new = seed_defaults_if_missing()
    if soul_new:
        _ok(f"created {SOUL_PATH}  (voice/tone — edit anytime)")
    else:
        _ok(f"using existing {SOUL_PATH}")
    if mem_new:
        _ok(f"created {MEMORY_PATH}  (durable facts — fill in payment, "
            f"address, preferences)")
    else:
        _ok(f"using existing {MEMORY_PATH}")
    print(_c("    Both files are full-injected into the prompt every run. "
             "Edit them in any text editor.", DIM))

    _step(6, 6, "Telegram inbound (optional — phone → Mac control)")
    configure_telegram()      # never blocks; returns even on skip

    print(_c("\nAll set.", GRN, BOLD) + " " + _c("Run", DIM) + " "
          + _c("openseer", CYN, BOLD) + " "
          + _c(f"to enter the chat shell. Provider: {provider}", DIM))
    print(_c("Run", DIM) + " " + _c("openseer daemon", CYN, BOLD) + " "
          + _c("to listen on Telegram (if configured).\n", DIM))
    return 0
