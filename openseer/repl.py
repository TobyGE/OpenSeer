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
from .agent import run, _default_callbacks
from .callbacks import Callback

# ─── ANSI colors (no external deps) ────────────────────────────────────────────
RESET, BOLD, DIM = "\x1b[0m", "\x1b[1m", "\x1b[2m"
RED, GRN, YEL, BLU, MAG, CYN = (f"\x1b[3{i}m" for i in range(1, 7))


def c(s: str, *codes: str) -> str:
    return f"{''.join(codes)}{s}{RESET}"


# ─── pretty per-step renderer (used as a Callback) ─────────────────────────────

def _shorten(s: str | None, n: int) -> str:
    if not s:
        return ""
    s = s.replace("\n", " ").strip()
    return s if len(s) <= n else s[: n - 1] + "…"


class _PrettyConsole(Callback):
    """Render each Step as a single compact bullet line, cc/codex-style."""

    name = "PrettyConsole"

    def on_step_recorded(self, ctx: dict, step) -> None:
        a = step.action
        # Each step is one line. Format:
        #   ● <thought truncated>          <action summary>          ✓
        bullet = c("●", CYN)

        # action summary
        if a.name == "open_app":
            act = f"open {a.app}"
        elif a.name == "click":
            act = f"click ({a.x},{a.y})"
        elif a.name == "double_click":
            act = f"double-click ({a.x},{a.y})"
        elif a.name == "type":
            text = _shorten(a.text, 40)
            act = f"type {text!r}" + (f" → ({a.x},{a.y})" if a.x is not None else "")
        elif a.name == "key":
            act = f"key {a.key}"
        elif a.name == "scroll":
            act = f"scroll ({a.x},{a.y}) amt={a.amount}"
        elif a.name == "wait":
            act = f"wait {a.amount}s"
        elif a.name == "reground":
            tag = "ext" if a.external else "default"
            act = f"reground[{tag}] {_shorten(a.target, 30)!r}"
        elif a.name == "done":
            act = c("done", GRN, BOLD) + " " + _shorten(a.reason, 60)
        elif a.name == "fail":
            act = c("fail", RED, BOLD) + " " + _shorten(a.reason, 60)
        elif a.name == "verify_failed":
            act = c("done rejected", YEL) + " — " + _shorten(a.reason, 60)
        else:
            act = a.name

        # status indicator
        result = (step.result or "").lower()
        if "fail" in result or "error" in result:
            mark = c("✗", RED)
        elif a.name in ("done", "fail", "verify_failed"):
            mark = ""
        else:
            mark = c("✓", GRN, DIM)

        # thought, dimmed and truncated
        thought = _shorten(a.thought, 70)
        thought_str = c(thought, DIM) if thought else ""

        # one-line render
        if thought_str:
            print(f"  {bullet} {thought_str}")
            print(f"     {DIM}└{RESET} {act} {mark}")
        else:
            print(f"  {bullet} {act} {mark}")


def _build_repl_callbacks() -> list[Callback]:
    cbs = _default_callbacks(quiet=True)
    cbs.append(_PrettyConsole())
    return cbs


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
    print("  --steps N        cap at N steps (default 20)")
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
    opts = {"max_steps": 20, "dry_run": False, "confirm_each": False}
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
    # one-line header, dimmed
    flags = []
    if opts["dry_run"]:    flags.append(c("dry", YEL))
    if opts["confirm_each"]: flags.append(c("confirm", YEL))
    if opts["max_steps"] != 20: flags.append(f"steps≤{opts['max_steps']}")
    flag_str = ("  " + " ".join(flags)) if flags else ""
    print(f"  {c('▸', BLU)} {c(task, BOLD)}{flag_str}")
    print()

    t0 = time.time()
    try:
        history = run(
            task,
            max_steps=opts["max_steps"],
            dry_run=opts["dry_run"],
            confirm_each=opts["confirm_each"],
            sleep_between=0.0,
            callbacks=_build_repl_callbacks(),
            quiet=True,
        )
    except KeyboardInterrupt:
        print(c("\n  ⏵ interrupted", YEL))
        return
    except Exception as e:
        print(c(f"\n  ✗ {e}", RED))
        return

    secs = time.time() - t0
    n = len(history)
    last = history[-1] if history else None
    status = last.action.name if last else "?"

    # totals were stashed by TrajectoryCallback
    totals = {}
    # find the trajectory ctx via callbacks isn't easy; use the latest run dir
    runs = sorted((Path.home() / "Desktop" / "openseer").glob("run-*"), reverse=True)
    out_dir = runs[0] if runs else None

    in_tok = sum((s.usage or {}).get("input_tokens", 0)  for s in history)
    out_tok = sum((s.usage or {}).get("output_tokens", 0) for s in history)
    cost = in_tok / 1e6 * 0.50 + out_tok / 1e6 * 4.00

    print()
    if status == "done":
        head = c("✓ done", GRN, BOLD)
    elif status in ("fail", "verify_failed"):
        head = c("⚠ " + status, YEL, BOLD)
    else:
        head = c("• cap", DIM, BOLD)
    summary = (f"  {head}  {n} step{'s' if n != 1 else ''} · {secs:.1f}s · "
               f"{c(f'{in_tok:,} in / {out_tok:,} out', DIM)} · "
               f"{c(f'~${cost:.3f}', DIM)}")
    print(summary)
    if last and last.action.reason:
        print(c(f"        → {_shorten(last.action.reason, 80)}", DIM))
    if out_dir:
        print(c(f"        ↳ {out_dir.name}", DIM))


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
