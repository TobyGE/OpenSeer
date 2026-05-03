"""Interactive REPL — `openseer` with no args drops into a chat shell.

Each line you type is a task; the agent runs it and reports back. Slash
commands handle non-task actions (status / history / exit).

Tasks are isolated from each other for now — each task starts a fresh
agent loop with empty conversation history. Continuing a task across
multiple inputs (true conversational memory) lives further down the
roadmap, alongside the PersonalMem bridge.
"""
from __future__ import annotations

import os
import readline
import sys
import time
from datetime import datetime
from pathlib import Path

from . import auth as auth_mod
from .agent import run

# ─── ANSI colors (no external deps) ────────────────────────────────────────────
RESET, BOLD, DIM = "\x1b[0m", "\x1b[1m", "\x1b[2m"
RED, GRN, YEL, BLU, MAG, CYN = (f"\x1b[3{i}m" for i in range(1, 7))


def c(s: str, *codes: str) -> str:
    return f"{''.join(codes)}{s}{RESET}"


HISTORY_FILE = Path.home() / ".openseer" / "repl-history"


def _setup_readline() -> None:
    HISTORY_FILE.parent.mkdir(parents=True, exist_ok=True)
    if HISTORY_FILE.exists():
        try:
            readline.read_history_file(HISTORY_FILE)
        except Exception:
            pass
    readline.set_history_length(1000)


def _save_history() -> None:
    try:
        readline.write_history_file(HISTORY_FILE)
    except Exception:
        pass


# ─── slash commands ────────────────────────────────────────────────────────────

def _cmd_help() -> None:
    print(c("Commands:", BOLD))
    print("  /help            show this help")
    print("  /status          show login state")
    print("  /history         list recent runs (Desktop/openseer/run-*)")
    print("  /clear           clear screen")
    print("  /exit, /quit     leave")
    print()
    print(c("Task syntax:", BOLD))
    print("  Just type what you want done. Example:")
    print(c("    Open Calculator and compute 999 * 123", DIM))
    print()
    print(c("Run options (suffix flags):", BOLD))
    print("  --dry            don't drive the UI, just predict")
    print("  --steps N        cap at N steps (default 12)")
    print("  --confirm        prompt y/s/q before each action")


def _cmd_status() -> None:
    st = auth_mod.token_status()
    print(c(st.summary(), BOLD if not st.expired and st.has_file else YEL))
    print(f"  codex CLI:  {auth_mod.codex_cli_path() or c('NOT INSTALLED', RED)}")
    print(f"  auth.json:  {auth_mod.AUTH_FILE}")


def _cmd_history(n: int = 10) -> None:
    runs = sorted((Path.home() / "Desktop" / "openseer").glob("run-*"),
                  reverse=True)[:n]
    if not runs:
        print(c("(no runs yet)", DIM))
        return
    for r in runs:
        task_file = r / "task.txt"
        task = task_file.read_text().strip().splitlines()[0][:80] if task_file.exists() else "?"
        print(f"  {c(r.name, CYN)}  {task}")


def _cmd_clear() -> None:
    os.system("clear" if os.name != "nt" else "cls")


# ─── input parsing ────────────────────────────────────────────────────────────

def _parse_task_flags(line: str) -> tuple[str, dict]:
    """Pull `--dry`, `--steps N`, `--confirm` off the END of the input.

    Examples:
      "open Notes --dry"          → ("open Notes", {dry_run: True})
      "open Notes --steps 8"      → ("open Notes", {max_steps: 8})
    """
    parts = line.split()
    opts = {"max_steps": 12, "dry_run": False, "confirm_each": False}
    keep = []
    i = 0
    while i < len(parts):
        p = parts[i]
        if p == "--dry":
            opts["dry_run"] = True
        elif p == "--confirm":
            opts["confirm_each"] = True
        elif p == "--steps" and i + 1 < len(parts) and parts[i + 1].isdigit():
            opts["max_steps"] = int(parts[i + 1])
            i += 1
        else:
            keep.append(p)
        i += 1
    return " ".join(keep).strip(), opts


# ─── one task run ──────────────────────────────────────────────────────────────

def _run_task(task: str, opts: dict) -> None:
    print(c(f"\n[task] {task}", DIM))
    if opts["dry_run"]:
        print(c("       (dry-run, no UI actions will execute)", DIM, YEL))
    print()
    t0 = time.time()
    try:
        history = run(
            task,
            max_steps=opts["max_steps"],
            dry_run=opts["dry_run"],
            confirm_each=opts["confirm_each"],
            sleep_between=0.0,
        )
    except KeyboardInterrupt:
        print(c("\n[interrupted]", YEL))
        return
    except Exception as e:
        print(c(f"\n[error] {e}", RED))
        return
    secs = time.time() - t0
    n = len(history)
    last = history[-1] if history else None
    status = last.action.name if last else "?"
    color = GRN if status == "done" else (YEL if status in ("fail", "verify_failed") else RED)
    print(c(f"\n[finished] {n} step(s) in {secs:.1f}s — last: {status}", color, BOLD))
    if last and last.action.reason:
        print(c(f"  → {last.action.reason}", DIM))


# ─── main loop ─────────────────────────────────────────────────────────────────

_BANNER = (
    f"{BOLD}OpenSeer{RESET} {DIM}— Sees. Remembers. Acts.{RESET}\n"
    f"  Type a task, or {CYN}/help{RESET} for commands, {CYN}/exit{RESET} to leave."
)


def _preflight() -> bool:
    """Check auth before starting the loop. Returns False if we should bail."""
    st = auth_mod.token_status()
    if not st.has_file:
        print(c("Not logged in.", RED))
        print(st.summary())
        print(f"\nRun {c('openseer auth login', CYN)} first.")
        return False
    if st.expired:
        print(c("Login expired.", YEL))
        print(st.summary())
        print(f"\nRun {c('openseer auth login', CYN)} to refresh.")
        return False
    return True


def repl() -> int:
    if not _preflight():
        return 1

    _setup_readline()
    print(_BANNER)
    print(c(f"  logged in: {auth_mod.token_status().plan_type or '?'}", DIM))
    print()

    try:
        while True:
            try:
                line = input(c("openseer ❯ ", BOLD, MAG))
            except EOFError:
                print()
                break
            line = line.strip()
            if not line:
                continue

            # slash commands
            if line.startswith("/"):
                cmd = line.split()[0].lower()
                if cmd in ("/exit", "/quit"):
                    break
                if cmd == "/help":
                    _cmd_help()
                elif cmd == "/status":
                    _cmd_status()
                elif cmd == "/history":
                    _cmd_history()
                elif cmd == "/clear":
                    _cmd_clear()
                else:
                    print(c(f"unknown command: {cmd}", RED))
                continue

            task, opts = _parse_task_flags(line)
            if not task:
                continue
            _run_task(task, opts)

    finally:
        _save_history()

    print(c("bye.", DIM))
    return 0
