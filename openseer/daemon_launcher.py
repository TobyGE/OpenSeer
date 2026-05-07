"""Launch a Terminal-hosted OpenSeer daemon from a launchd-safe wrapper.

macOS GUI automation permissions are tied to the interactive app that hosts
the Python process. A LaunchAgent can keep a watcher alive, but running the
computer-use daemon directly under launchd may lose reliable Screen Recording /
Accessibility / Automation access. This launcher is intentionally tiny: launchd
runs it, it opens Terminal with a command script, and the real daemon runs
inside Terminal.
"""
from __future__ import annotations

import os
import shlex
import subprocess
import sys
from pathlib import Path


HOME = Path.home()
RUN_SCRIPT = HOME / ".openseer" / "run-daemon.command"


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _openseer_bin() -> Path:
    exe = Path(sys.argv[0])
    if exe.exists():
        return exe.resolve()
    return _repo_root() / ".venv" / "bin" / "openseer"


def _terminal_daemon_pids() -> list[int]:
    """Return openseer daemon processes attached to an interactive tty."""
    try:
        r = subprocess.run(
            ["ps", "-axo", "pid=,tty=,command="],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except Exception:
        return []
    out: list[int] = []
    me = os.getpid()
    for line in (r.stdout or "").splitlines():
        parts = line.strip().split(None, 2)
        if len(parts) < 3:
            continue
        try:
            pid = int(parts[0])
        except ValueError:
            continue
        tty, cmd = parts[1], parts[2]
        if pid == me or tty == "??":
            continue
        if "openseer" in cmd and " daemon" in cmd and "daemon-launcher" not in cmd:
            out.append(pid)
    return out


def write_run_script() -> Path:
    RUN_SCRIPT.parent.mkdir(parents=True, exist_ok=True)
    root = _repo_root()
    openseer = _openseer_bin()
    log = HOME / ".openseer" / "terminal-daemon.log"
    body = f"""#!/bin/zsh
set -e
cd {shlex.quote(str(root))}
export OPENSEER_SKILL_UPDATES="${{OPENSEER_SKILL_UPDATES:-trace-only}}"
export PYTHONUNBUFFERED=1
echo "[openseer] Terminal-hosted daemon starting at $(date)"
echo "[openseer] cwd={shlex.quote(str(root))}"
echo "[openseer] binary={shlex.quote(str(openseer))}"
exec {shlex.quote(str(openseer))} daemon 2>&1 | tee -a {shlex.quote(str(log))}
"""
    RUN_SCRIPT.write_text(body, encoding="utf-8")
    RUN_SCRIPT.chmod(0o755)
    return RUN_SCRIPT


def launch_terminal_daemon() -> int:
    pids = _terminal_daemon_pids()
    if pids:
        print(f"daemon-launcher: Terminal daemon already running: {pids}")
        return 0
    script = write_run_script()
    print(f"daemon-launcher: opening Terminal with {script}")
    subprocess.run(["/usr/bin/open", "-a", "Terminal", str(script)], check=False)
    return 0


def main() -> int:
    return launch_terminal_daemon()
