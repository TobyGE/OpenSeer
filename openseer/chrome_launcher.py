"""Launch the user's real Chrome with --remote-debugging-port so
OpenSeer's CDP path can attach to THE BROWSER THEY ACTUALLY USE,
inheriting all their logins / extensions / tabs zero-config.

This is the "attach mode" alternative to OpenSeer's sandbox Chrome
(which lives in ~/.openseer/chrome-profile and gets a snapshot of
the user's profile copied in on first launch). Attach mode is:

  - One Chrome process instead of two (less RAM)
  - Real-time login state (no profile snapshot or refresh needed)
  - Agent operations visible in the user's own window (a feature
    for some, a distraction for others — we open scratch tabs in
    a NEW Chrome window placed in the background where possible)

The catch: macOS Dock can't pass `--remote-debugging-port` to apps.
The user must either:

  1. Launch Chrome once via `openseer chrome` (this module)
  2. Set up a Login Item that runs (1) at every macOS login —
     see install_login_item() below

After that, port 9222 stays open as long as Chrome runs, OpenSeer
attaches automatically.
"""
from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path
from typing import Any


_DEFAULT_PORT = 9222
_CHROME_APP = "Google Chrome"
_LOGIN_ITEM_LABEL = "com.openseer.chrome-launcher"
_LOGIN_ITEM_PLIST = (
    Path.home() / "Library" / "LaunchAgents"
    / f"{_LOGIN_ITEM_LABEL}.plist"
)


# ── process detection ───────────────────────────────────────────────


def _chrome_running() -> tuple[bool, list[int]]:
    """Return (is_running, [pids]) for the user's main Chrome
    process. Skips helper subprocesses (those carry --type=...)."""
    try:
        r = subprocess.run(
            ["pgrep", "-fl",
             "Google Chrome.app/Contents/MacOS/Google Chrome"],
            capture_output=True, text=True, timeout=3.0)
    except (subprocess.SubprocessError, OSError):
        return (False, [])
    if r.returncode != 0:
        return (False, [])
    pids: list[int] = []
    for line in r.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        if "--type=" in line:
            continue
        try:
            pid = int(line.split(None, 1)[0])
        except ValueError:
            continue
        pids.append(pid)
    return (bool(pids), pids)


def _chrome_running_with_flag(port: int = _DEFAULT_PORT
                               ) -> tuple[bool, int | None]:
    """Is the user's Chrome currently running AND exposing the CDP
    endpoint on ``port``? We need both: pid liveness AND a
    `/json/version` probe that confirms the listener is actually
    Chrome DevTools (not some other process that happens to have
    grabbed the port — Cursor's debug-port, a Puppeteer leftover,
    a dev-server, …)."""
    running, pids = _chrome_running()
    if not running:
        return (False, None)
    import urllib.request as _ur
    import urllib.error as _ue
    try:
        with _ur.urlopen(
                f"http://127.0.0.1:{port}/json/version",
                timeout=0.5) as r:
            if r.status != 200:
                return (False, pids[0])
            body = r.read().decode("utf-8", errors="replace")
    except (_ue.URLError, OSError, TimeoutError):
        return (False, pids[0] if pids else None)
    # Confirm the JSON shape looks like Chrome's /json/version output —
    # any HTTP service that responds 200 to a random GET would pass a
    # naked socket probe but won't have "Browser" / "webSocketDebuggerUrl"
    # fields.
    if '"Browser"' not in body \
            or '"webSocketDebuggerUrl"' not in body:
        return (False, pids[0])
    return (True, pids[0])


def status(port: int = _DEFAULT_PORT) -> dict[str, Any]:
    """Snapshot the current state for `openseer chrome status` /
    Swift Settings UI's status indicator."""
    running, pids = _chrome_running()
    with_flag, flag_pid = _chrome_running_with_flag(port)
    return {
        "chrome_running": running,
        "chrome_pids": pids,
        "cdp_port_open": with_flag,
        "cdp_port": port if with_flag else None,
        "login_item_installed": _LOGIN_ITEM_PLIST.is_file(),
    }


# ── launch / relaunch ──────────────────────────────────────────────


def launch_with_flag(port: int = _DEFAULT_PORT,
                      *, restart_if_running: bool = False,
                      profile_directory: str | None = None,
                      wait_seconds: float = 8.0) -> dict[str, Any]:
    """Launch (or relaunch) the user's Chrome with
    ``--remote-debugging-port=<port>``.

    If Chrome is already running and ``restart_if_running=False``,
    we DON'T quit it — Chrome ignores command-line args on the
    second `open` so we'd silently succeed without the flag.
    Instead we return ``status="already_running"`` and let the
    caller decide whether to ask the user to retry with the
    restart flag.

    With ``restart_if_running=True``, we ask Chrome to quit first
    via AppleScript (allows the user's tabs to be checkpointed),
    wait for it to die, then `open -na` with the flag. Chrome's
    session-restore picks up the tabs on the new launch.

    Returns a status dict so CLI / Swift can render meaningfully.
    """
    running, pids = _chrome_running()
    if running and not restart_if_running:
        # Check if it ALREADY has the flag — if yes, nothing to do.
        with_flag, _ = _chrome_running_with_flag(port)
        if with_flag:
            return {
                "status": "already_running_with_flag",
                "pid": pids[0], "port": port,
            }
        return {
            "status": "running_without_flag",
            "pid": pids[0], "port": port,
            "hint": (
                "Chrome is running but wasn't launched with "
                f"--remote-debugging-port={port}. Pass "
                "restart_if_running=True (or run `openseer chrome "
                "restart`) to quit + relaunch with the flag. "
                "Your tabs will be restored on the new launch."),
        }
    if running and restart_if_running:
        try:
            subprocess.run(
                ["osascript", "-e",
                 f'tell application "{_CHROME_APP}" to quit'],
                capture_output=True, timeout=5.0)
        except (subprocess.SubprocessError, OSError):
            pass
        # Wait up to 5s for Chrome to actually exit (it needs to
        # checkpoint its DBs). pgrep returns empty when it's gone.
        deadline = time.time() + 5.0
        while time.time() < deadline:
            still_running, _ = _chrome_running()
            if not still_running:
                break
            time.sleep(0.2)

    # `open -na` opens a NEW instance with the args. Crucially
    # `-n` (new) overrides Launch Services' "reuse running" default.
    args = ["open", "-na", _CHROME_APP, "--args",
             f"--remote-debugging-port={port}"]
    if profile_directory:
        args.append(f"--profile-directory={profile_directory}")
    try:
        subprocess.run(args, capture_output=True, timeout=8.0)
    except (subprocess.SubprocessError, OSError) as e:
        return {"status": "launch_failed", "error": str(e)}

    # Wait for the port to start answering — Chrome takes ~1-3s
    # to spawn + listen, longer if restoring many tabs.
    deadline = time.time() + wait_seconds
    while time.time() < deadline:
        with_flag, pid = _chrome_running_with_flag(port)
        if with_flag:
            return {
                "status": "launched",
                "pid": pid, "port": port,
            }
        time.sleep(0.3)
    return {
        "status": "launched_but_port_silent",
        "port": port,
        "hint": (
            f"Chrome was launched but port {port} didn't answer "
            f"within {wait_seconds}s. Chrome may still be restoring "
            "tabs — wait a moment and try again."),
    }


# ── Login Item plist (LaunchAgent) ─────────────────────────────────


def _plist_xml(port: int = _DEFAULT_PORT) -> str:
    """LaunchAgent plist that runs `open -na Chrome --args
    --remote-debugging-port=<port>` exactly once at user login."""
    return f'''<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>{_LOGIN_ITEM_LABEL}</string>
    <key>ProgramArguments</key>
    <array>
        <string>/usr/bin/open</string>
        <string>-na</string>
        <string>{_CHROME_APP}</string>
        <string>--args</string>
        <string>--remote-debugging-port={port}</string>
    </array>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <false/>
</dict>
</plist>
'''


def install_login_item(port: int = _DEFAULT_PORT) -> dict[str, Any]:
    """Write the LaunchAgent plist + register it with launchd so
    Chrome auto-launches with the debug flag at every macOS login.

    Idempotent: overwrites the plist if it already exists. The
    user can `openseer chrome --disable-login-item` to remove it.
    """
    _LOGIN_ITEM_PLIST.parent.mkdir(parents=True, exist_ok=True)
    _LOGIN_ITEM_PLIST.write_text(_plist_xml(port), encoding="utf-8")
    # `launchctl bootstrap` is the modern, well-behaved way to load
    # an agent into the current user's gui session. We tear down
    # any prior version first so the agent reloads cleanly on a
    # port-change.
    uid = os.getuid()
    try:
        subprocess.run(
            ["launchctl", "bootout", f"gui/{uid}",
             str(_LOGIN_ITEM_PLIST)],
            capture_output=True, timeout=5.0)
    except (subprocess.SubprocessError, OSError):
        pass
    try:
        r = subprocess.run(
            ["launchctl", "bootstrap", f"gui/{uid}",
             str(_LOGIN_ITEM_PLIST)],
            capture_output=True, text=True, timeout=5.0)
    except (subprocess.SubprocessError, OSError) as e:
        return {"status": "failed",
                "error": f"launchctl bootstrap: {e}",
                "plist": str(_LOGIN_ITEM_PLIST)}
    if r.returncode != 0:
        return {"status": "failed",
                "error": (r.stderr or "").strip()
                          or f"launchctl returned {r.returncode}",
                "plist": str(_LOGIN_ITEM_PLIST)}
    return {"status": "installed",
            "plist": str(_LOGIN_ITEM_PLIST),
            "port": port}


def uninstall_login_item() -> dict[str, Any]:
    """Tear down the LaunchAgent so Chrome no longer auto-launches
    with the debug flag at login. The agent stops immediately;
    any Chrome it already launched keeps running."""
    if not _LOGIN_ITEM_PLIST.is_file():
        return {"status": "not_installed"}
    uid = os.getuid()
    try:
        subprocess.run(
            ["launchctl", "bootout", f"gui/{uid}",
             str(_LOGIN_ITEM_PLIST)],
            capture_output=True, timeout=5.0)
    except (subprocess.SubprocessError, OSError):
        pass
    try:
        _LOGIN_ITEM_PLIST.unlink()
    except OSError as e:
        return {"status": "failed", "error": str(e)}
    return {"status": "uninstalled",
            "plist": str(_LOGIN_ITEM_PLIST)}
