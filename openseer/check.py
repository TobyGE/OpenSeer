"""``openseer check --json`` — single-shot system probe.

The macOS GUI's setup wizard wants every status flag in one shot
(provider logins, TCC permissions, Telegram config) so it can render
all five wizard steps without firing five separate subprocesses.

We deliberately keep this module side-effect free (no logins, no
permission prompts, no file writes). It's pure introspection: the
GUI calls it once on each wizard step transition, gets a JSON blob,
binds the relevant fields, and shows the user what to do next.

Output schema (sample):

    {
      "version": 1,
      "providers": {
        "openai":    {"logged_in": true,  "expires_in_s": 12345,
                      "plan": "plus"},
        "anthropic": {"logged_in": false, "expires_in_s": 0,
                      "subscription": null,
                      "error": "Claude Code OAuth not found ..."}
      },
      "selected_provider": "openai",
      "permissions": {
        "accessibility":    true,
        "screen_recording": true
      },
      "telegram": {
        "configured": false,
        "enabled":    false,
        "token_present": false,
        "allowed_chat_ids":     [],
        "trigger_prefix":       "",
        "max_steps":            null,
        "step_check_interval":  null
      },
      "binary_paths": {
        "codex":  "/usr/local/bin/codex",
        "claude": null
      }
    }

Fields that are not yet implementable on this machine return safe
defaults rather than raising — the GUI handles `false` / `null` /
empty strings as "needs setup".
"""
from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

from . import auth as _codex_auth


def _openai_status() -> dict[str, Any]:
    """Codex / ChatGPT OAuth via ~/.codex/auth.json."""
    try:
        st = _codex_auth.token_status()
    except Exception as e:  # pragma: no cover - defensive
        return {"logged_in": False, "expires_in_s": 0, "error": str(e)}
    if not st.has_file:
        return {"logged_in": False, "expires_in_s": 0,
                "error": "Codex CLI not signed in (~/.codex/auth.json missing)."}
    out: dict[str, Any] = {
        "logged_in": not st.expired,
        "plan": st.plan_type,
        "expires_in_s": int((st.expires_at or 0) - _now()) if st.expires_at else 0,
    }
    if st.expired:
        out["error"] = "ChatGPT OAuth token expired — run `openseer auth login`."
    return out


def _anthropic_status() -> dict[str, Any]:
    """Claude Code OAuth via macOS Keychain (`Claude Code-credentials`)."""
    try:
        from . import anthropic_messages as _ant
    except Exception as e:  # pragma: no cover - non-mac envs
        return {"logged_in": False, "expires_in_s": 0,
                "error": f"anthropic_messages import failed: {e}"}
    try:
        s = _ant.token_status()
    except Exception as e:
        return {"logged_in": False, "expires_in_s": 0, "error": str(e)}
    if not s.get("present"):
        return {"logged_in": False, "expires_in_s": 0,
                "subscription": None,
                "error": s.get("error")
                         or "Claude Code OAuth not found in macOS Keychain."}
    expires_in = int(s.get("expires_in_s", 0))
    return {
        "logged_in": expires_in > 0,
        "expires_in_s": expires_in,
        "subscription": s.get("subscription"),
        "error": None if expires_in > 0 else "Claude Code OAuth expired.",
    }


def _selected_provider() -> str | None:
    """Same resolution agent.run uses, surfaced for the GUI."""
    try:
        from .agent import _resolve_provider
        return _resolve_provider()
    except Exception:
        return None


def _permissions() -> dict[str, bool]:
    """Non-interactive probes for Accessibility + Screen Recording.

    Both are PyObjC calls to the standard macOS APIs:
      - AXIsProcessTrusted()              for Accessibility
      - CGPreflightScreenCaptureAccess()  for Screen Recording

    The CLI wizard's `check_accessibility` is interactive (prompts the
    user to press Enter, nudges the cursor, asks for visual
    confirmation) — that doesn't suit a GUI status probe. The APIs
    here just report the current TCC state for THIS process, no UI.

    Note: TCC is per-process, so the Swift app, the Python REPL, the
    Telegram daemon, and `openseer` (running this check) each have
    their OWN permission state. The GUI shows what THIS check
    process sees, which corresponds to whatever app spawned the
    `openseer check` subprocess. For the daemon's actual control
    needs, the daemon process must also have the permissions —
    typically the user's terminal app, granted via System Settings.
    """
    out = {"accessibility": False, "screen_recording": False}
    try:
        from ApplicationServices import AXIsProcessTrusted  # type: ignore[import-untyped]
        out["accessibility"] = bool(AXIsProcessTrusted())
    except Exception:
        pass
    try:
        from Quartz import CGPreflightScreenCaptureAccess  # type: ignore[import-untyped]
        out["screen_recording"] = bool(CGPreflightScreenCaptureAccess())
    except Exception:
        pass
    return out


def _telegram() -> dict[str, Any]:
    """Read the persisted Telegram block from ~/.openseer/config.json.
    No secrets returned — only whether the token is set, plus the
    non-secret config knobs."""
    cfg_path = Path.home() / ".openseer" / "config.json"
    if not cfg_path.exists():
        return {"configured": False, "enabled": False, "token_present": False,
                "allowed_chat_ids": [], "trigger_prefix": "",
                "max_steps": None, "step_check_interval": None}
    try:
        cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    except Exception:
        return {"configured": False, "enabled": False, "token_present": False,
                "allowed_chat_ids": [], "trigger_prefix": "",
                "max_steps": None, "step_check_interval": None}
    tg = cfg.get("telegram") or {}
    return {
        "configured": bool(tg),
        "enabled": bool(tg.get("enabled")),
        "token_present": bool(tg.get("token")),
        "allowed_chat_ids": list(tg.get("allowed_chat_ids") or []),
        "trigger_prefix": tg.get("trigger_prefix") or "",
        "max_steps": tg.get("max_steps"),
        "step_check_interval": tg.get("step_check_interval"),
    }


def _binary_paths() -> dict[str, str | None]:
    return {
        "codex":  shutil.which("codex"),
        "claude": shutil.which("claude"),
    }


def _now() -> float:
    import time
    return time.time()


def collect() -> dict[str, Any]:
    """Build the full status blob. Safe to call from any subcommand."""
    return {
        "version": 1,
        "providers": {
            "openai":    _openai_status(),
            "anthropic": _anthropic_status(),
        },
        "selected_provider": _selected_provider(),
        "permissions": _permissions(),
        "telegram":    _telegram(),
        "binary_paths": _binary_paths(),
    }


def request_permissions() -> int:
    """Trigger the TCC prompts FROM THIS python process so macOS
    adds it to the Accessibility / Screen-Recording Privacy lists.
    The bundled .app's TCC grants don't transfer to the python
    child; the user has to grant them to python directly. macOS
    only surfaces a process in those lists once it's actually
    called the relevant API, which is what we do here.

    Idempotent: calling twice is harmless; if already granted, the
    APIs return immediately without any UI.
    """
    granted_ax = False
    granted_sr = False
    try:
        # AXIsProcessTrustedWithOptions(prompt=true) — opens the
        # "OpenSeer wants to control your computer" dialog and
        # registers this process in the AX list.
        from ApplicationServices import (  # type: ignore[import-untyped]
            AXIsProcessTrustedWithOptions,
            kAXTrustedCheckOptionPrompt,
        )
        from CoreFoundation import CFDictionaryCreate, kCFTypeDictionaryKeyCallBacks, kCFTypeDictionaryValueCallBacks  # type: ignore[import-untyped]
        opts = {kAXTrustedCheckOptionPrompt: True}
        granted_ax = bool(AXIsProcessTrustedWithOptions(opts))
    except Exception as e:
        print(f"  [perms] accessibility request failed: {e!r}")
    try:
        # CGRequestScreenCaptureAccess prompts AND registers in the
        # Screen Recording list. First call is the magic one — it
        # makes the row appear in System Settings so the user can
        # toggle it on.
        from Quartz import CGRequestScreenCaptureAccess  # type: ignore[import-untyped]
        granted_sr = bool(CGRequestScreenCaptureAccess())
    except Exception as e:
        print(f"  [perms] screen-recording request failed: {e!r}")
    print(f"accessibility:    {'granted' if granted_ax else 'pending — toggle in System Settings'}")
    print(f"screen recording: {'granted' if granted_sr else 'pending — toggle in System Settings'}")
    return 0


def main(json_out: bool = True) -> int:
    """CLI entry. ``json_out=True`` prints a single JSON object;
    ``False`` is a brief human summary for terminal use."""
    blob = collect()
    if json_out:
        print(json.dumps(blob, ensure_ascii=False, indent=2))
        return 0
    # Human summary
    p = blob["providers"]
    print(f"selected provider: {blob.get('selected_provider') or '?'}")
    print(f"  openai:    "
          f"{'✓ logged in' if p['openai']['logged_in'] else '✗ ' + (p['openai'].get('error') or 'not logged in')}")
    print(f"  anthropic: "
          f"{'✓ logged in' if p['anthropic']['logged_in'] else '✗ ' + (p['anthropic'].get('error') or 'not logged in')}")
    perms = blob["permissions"]
    print(f"permissions:")
    print(f"  accessibility:    {'✓' if perms['accessibility'] else '✗'}")
    print(f"  screen_recording: {'✓' if perms['screen_recording'] else '✗'}")
    tg = blob["telegram"]
    if tg["configured"]:
        n = len(tg["allowed_chat_ids"])
        print(f"telegram: token={'set' if tg['token_present'] else 'missing'}, "
              f"{'enabled' if tg['enabled'] else 'disabled'}, "
              f"{n} allowed chat{'s' if n != 1 else ''}")
    else:
        print(f"telegram: not configured")
    return 0
