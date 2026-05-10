"""Factory-reset OpenSeer: wipe OAuth tokens, configs, and TCC grants.

Used by the GUI's "Re-run setup" button so a click brings the user
back to a clean-machine state — everything that the setup wizard
would otherwise have to detect-and-tolerate gets cleared.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

# What we wipe, plus a short label for the report. We never touch
# SOUL.md / MEMORY.md / skills/ — those are user-authored content
# and would be infuriating to lose to a setup re-run.
_FILES = [
    Path.home() / ".codex" / "auth.json",                  # OpenAI OAuth
    Path.home() / ".openseer" / "config.json",             # provider + telegram cfg
    Path.home() / ".openseer" / "inbox" / "sessions.json", # per-chat memory
]
_KEYCHAIN_SERVICES = [
    "Claude Code-credentials",   # Anthropic OAuth (Claude Code keychain)
]
# TCC services we registered at runtime via the Permissions probes.
# Reset for our bundle id; if user moved the .app or has a different
# id, these are no-ops.
_TCC_SERVICES = [
    "Accessibility",
    "ScreenCapture",
    "AppleEvents",
    "SystemPolicyAllFiles",
    "Microphone",
    "Camera",
]
_BUNDLE_ID = "com.openseer.OpenSeer"


def run() -> int:
    """Run the full reset. Returns 0 on success (non-fatal individual
    failures are reported but don't fail the whole reset)."""
    summary: list[str] = []

    for f in _FILES:
        if f.exists():
            try:
                f.unlink()
                summary.append(f"deleted {f}")
            except Exception as e:
                summary.append(f"FAILED to delete {f}: {e}")
        else:
            summary.append(f"absent     {f}")

    # Use PyObjC Security framework instead of `security
    # delete-generic-password`. Delete doesn't take a password
    # argument, so argv exposure isn't an issue here, but keeping
    # the keychain path consistent with the OAuth save path
    # simplifies things and lets us run without needing the
    # `security` binary on $PATH.
    try:
        from Security import (  # type: ignore[import-untyped]
            SecItemDelete, kSecClass, kSecClassGenericPassword,
            kSecAttrService, errSecItemNotFound,
        )
    except ImportError:
        SecItemDelete = None
    for svc in _KEYCHAIN_SERVICES:
        if SecItemDelete is None:
            # Fallback to the CLI when the Security framework
            # isn't bundled (older install / dev environment).
            r = subprocess.run(
                ["security", "delete-generic-password", "-s", svc],
                capture_output=True, text=True)
            if r.returncode == 0:
                summary.append(f"keychain wiped: {svc}")
            else:
                err = (r.stderr or r.stdout).strip()
                if "could not be found" in err.lower():
                    summary.append(f"keychain absent: {svc}")
                else:
                    summary.append(f"keychain FAIL  {svc}: {err}")
            continue
        status = SecItemDelete({
            kSecClass:       kSecClassGenericPassword,
            kSecAttrService: svc,
        })
        if status == 0:
            summary.append(f"keychain wiped: {svc}")
        elif status == errSecItemNotFound:
            summary.append(f"keychain absent: {svc}")
        else:
            summary.append(f"keychain FAIL  {svc}: OSStatus {status}")

    # Reset the .app bundle only. python-build-standalone registers
    # under `org.python.python`, which is SHARED with every other
    # CPython on this machine — resetting it would silently revoke
    # Privacy grants for unrelated Python apps the user has set up
    # (codex P2). The user can clear that entry manually in System
    # Settings if they want a truly nuclear reset; the GUI's confirm
    # alert mentions the caveat.
    targets = [_BUNDLE_ID]
    for tcc in _TCC_SERVICES:
        for tgt in targets:
            r = subprocess.run(
                ["tccutil", "reset", tcc, tgt],
                capture_output=True, text=True)
            label = f"tcc {tcc} / {tgt}"
            if r.returncode == 0:
                summary.append(f"reset {label}")
            else:
                err = (r.stderr or r.stdout).strip()
                # `No such bundle identifier` is harmless — means
                # that target never had a TCC entry.
                if "no such" in err.lower() or "not found" in err.lower():
                    summary.append(f"absent {label}")
                else:
                    summary.append(f"FAIL  {label}: {err}")

    print("\n".join(summary))
    return 0
