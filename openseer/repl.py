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
import json
import sys
import time
from datetime import datetime
from pathlib import Path

from dataclasses import dataclass

from . import auth as auth_mod
from .agent import run, _default_callbacks
from .callbacks import Callback
from .events import EventType

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
    """Progressive REPL renderer driven by TaskEvents.

    UX intent: never leave the user staring at a blank terminal during
    a slow model call or a long-running bash command. Show the current
    phase as a transient status line that's overwritten in place when
    the next event arrives, then commit a permanent bullet line per
    step once the result is in.
    """

    name = "PrettyConsole"

    def __init__(self) -> None:
        self._transient_active = False    # is a \r-style line currently on screen?
        self._model_t0: float | None = None

    # ─── transient status helpers ───────────────────────────────────────
    def _transient(self, text: str) -> None:
        sys.stdout.write("\r\033[2K  " + text)
        sys.stdout.flush()
        self._transient_active = True

    def _commit(self) -> None:
        """Drop any transient line so the next print() lands on a clean row."""
        if self._transient_active:
            sys.stdout.write("\r\033[2K")
            sys.stdout.flush()
            self._transient_active = False

    # ─── event router ───────────────────────────────────────────────────
    def on_event(self, ctx: dict, event) -> None:
        et = event.type

        if et == EventType.MODEL_STARTED:
            self._model_t0 = time.time()
            self._transient(c("⏳ thinking …", DIM))
            return

        if et == EventType.MODEL_FINISHED:
            self._model_t0 = None
            usage = event.get("usage") or {}
            in_t = usage.get("input_tokens", 0)
            out_t = usage.get("output_tokens", 0)
            self._transient(c(f"⌁ model ({event.get('elapsed_ms', 0)}ms · {in_t}+{out_t}t)", DIM))
            return

        if et == EventType.ACTION_STARTED:
            name = event.get("name", "?")
            summary = event.get("summary", "")
            self._transient(f"{c('▶', YEL)} {name} {c(summary, DIM)}")
            return

        if et == EventType.ACTION_FINISHED:
            # transient cleanup; the permanent line lands on STEP_RECORDED
            # (which fires AFTER history.append, ensuring _render_action_line
            # reads the just-recorded step, not a stale one)
            return

        if et == EventType.STEP_RECORDED:
            self._commit()
            self._render_action_line(event, ctx)
            return

        if et == EventType.SAFETY_BLOCKED:
            self._commit()
            print(f"  {c('✗', RED)} safety blocked: {c(event.get('reason', ''), RED)}")
            return

        if et == EventType.TASK_FAILED:
            self._commit()
            print(f"  {c('✗', RED)} task failed: {event.get('error', '')}")
            return

    def _render_action_line(self, event, ctx) -> None:
        """One-line bullet for a completed action, mirroring the previous
        post-step rendering style but driven by the action_finished event."""
        history = ctx.get("history") or []
        if not history:
            return
        s = history[-1]
        a = s.action
        bullet = c("●", CYN)

        if a.name == "open_app":
            act = f"open {a.app}"
        elif a.name == "click":
            act = f"click ({a.x},{a.y})" + (f" ×{a.count}" if a.count > 1 else "")
        elif a.name == "type":
            text = _shorten(a.text, 40)
            act = f"type {text!r}" + (f" → ({a.x},{a.y})" if a.x is not None else "")
        elif a.name == "key":
            act = f"key {a.key}"
        elif a.name == "scroll":
            act = f"scroll ({a.x},{a.y}) amt={a.amount}"
        elif a.name == "wait":
            act = f"wait {a.amount}s"
        elif a.name == "bash":
            act = f"bash {_shorten(a.cmd, 60)!r}"
        elif a.name == "reground":
            tag = "ext" if a.external else "default"
            act = f"reground[{tag}] {_shorten(a.target, 30)!r}"
        elif a.name == "terminate":
            st = (a.status or "done").lower()
            color = GRN if st == "done" else YEL
            act = c(f"terminate ({st})", color, BOLD) + " " + _shorten(a.reason, 60)
        elif a.name in ("done", "fail"):
            act = c(a.name, GRN if a.name == "done" else RED, BOLD) + " " + _shorten(a.reason, 60)
        elif a.name == "verify_failed":
            act = c("done rejected", YEL) + " — " + _shorten(a.reason, 60)
        else:
            act = a.name or "<empty>"

        result = (s.result or "").lower()
        if any(k in result for k in ("fail", "error", "reject", "block")):
            mark = c("✗", RED)
        elif a.name in ("done", "fail", "verify_failed", "terminate"):
            mark = ""
        else:
            mark = c("✓", GRN, DIM)

        thought = _shorten(a.thought, 70)
        if thought:
            print(f"  {bullet} {c(thought, DIM)}")
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

_RUNS_ROOT = Path.home() / ".openseer" / "runs"


def _cmd_help() -> None:
    print(c("Commands:", BOLD))
    print("  /help                 show this help")
    print("  /status               show login state")
    print("  /history [N]          list N most recent runs (default 10)")
    print("  /show [last|<id>|<n>] show details of a run (default: last)")
    print("  /open [last|<id>]     reveal the run dir in Finder")
    print("  /context              print current session memory the model sees")
    print("  /reset                clear session memory")
    print("  /clear                clear screen")
    print("  /exit, /quit          leave")
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


def _list_runs(limit: int | None = None) -> list[Path]:
    """Return run directories sorted newest-first. Skips the `latest` symlink."""
    if not _RUNS_ROOT.exists():
        return []
    candidates = [
        p for p in _RUNS_ROOT.iterdir()
        if p.is_dir() and not p.is_symlink() and p.name != "latest"
    ]
    candidates.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return candidates[:limit] if limit else candidates


def _resolve_run(arg: str | None) -> Path | None:
    """Resolve a `/show` / `/open` argument to a run directory.

    Resolution order:
      1. 'last' / empty → newest run via the `latest` symlink
      2. exact directory match (full trace_id)
      3. small all-digit arg (1..N) → Nth most recent (1-indexed)
      4. prefix match against trace_id (so 'a3c8' picks 'a3c8f120')

    Trace IDs are random hex and can start with digits, but a SHORT
    all-digit arg ("1", "3", "10") is overwhelmingly more likely to
    mean "Nth most recent" than to be a valid trace-ID prefix. Long
    all-digit prefixes are still caught by step 4 because numeric
    indices > 1000 are vanishingly rare.
    """
    if not arg or arg.lower() == "last":
        latest = _RUNS_ROOT / "latest"
        if latest.is_symlink() and latest.exists():
            return latest.resolve()
        runs = _list_runs(limit=1)
        return runs[0] if runs else None

    # 2: exact full trace_id
    candidate = _RUNS_ROOT / arg
    if candidate.is_dir() and not candidate.is_symlink():
        return candidate

    # 3: small all-digit → numeric index (Nth most recent)
    if arg.isdigit() and len(arg) <= 3:
        idx = int(arg) - 1
        runs = _list_runs()
        return runs[idx] if 0 <= idx < len(runs) else None

    # 4: prefix match (handles short hex prefixes AND long digit-prefixes)
    prefix = arg.lower()
    for r in _list_runs():
        if r.name.startswith(prefix):
            return r
    return None


def _cmd_history(n: int = 10) -> None:
    runs = _list_runs(limit=n)
    if not runs:
        print(c("(no runs yet)", DIM))
        return
    for r in runs:
        task = "?"
        status = "?"
        # prefer the new task.json + final.json; fall back to task.txt
        task_json = r / "task.json"
        final_json = r / "final.json"
        if task_json.exists():
            try:
                t = json.loads(task_json.read_text())
                task = (t.get("task") or "?").splitlines()[0][:80]
            except Exception:
                pass
        elif (r / "task.txt").exists():
            task = (r / "task.txt").read_text().strip().splitlines()[0][:80]
        if final_json.exists():
            try:
                status = json.loads(final_json.read_text()).get("status", "?")
            except Exception:
                pass
        sc = GRN if status == "done" else (YEL if status in ("fail", "verify_failed") else DIM)
        print(f"  {c(r.name, CYN)}  {c(status, sc)}  {task}")


def _cmd_show(arg: str | None) -> None:
    run_dir = _resolve_run(arg)
    if run_dir is None:
        print(c(f"no run matching {arg!r}", RED))
        return

    # Header
    task_meta = {}
    if (run_dir / "task.json").exists():
        try:
            task_meta = json.loads((run_dir / "task.json").read_text())
        except Exception:
            pass
    final_meta = {}
    if (run_dir / "final.json").exists():
        try:
            final_meta = json.loads((run_dir / "final.json").read_text())
        except Exception:
            pass

    print()
    print(f"  {c('trace:', BOLD)} {c(run_dir.name, CYN)}  "
          f"{c(task_meta.get('model', ''), DIM)}")
    print(f"  {c('task:', BOLD)} {task_meta.get('task', '?')}")
    if final_meta:
        st = final_meta.get("status", "?")
        sc = GRN if st == "done" else (YEL if st in ("fail", "verify_failed") else DIM)
        totals = final_meta.get("totals") or {}
        n_steps = final_meta.get("n_steps", 0)
        in_tok = totals.get("input_tokens", 0)
        out_tok = totals.get("output_tokens", 0)
        steps_label = c(f"{n_steps} steps", DIM)
        tok_label = c(f"{in_tok:,} in / {out_tok:,} out", DIM)
        print(f"  {c('status:', BOLD)} {c(st, sc, BOLD)}  {steps_label}  {tok_label}")
        if final_meta.get("last_reason"):
            print(f"  {c('reason:', BOLD)} {final_meta['last_reason']}")

    # Per-step lines from transcript.json
    transcript = run_dir / "transcript.json"
    if transcript.exists():
        try:
            data = json.loads(transcript.read_text())
            print()
            for s in data.get("steps", []):
                _render_step_line(s)
        except Exception as e:
            print(c(f"  (transcript unreadable: {e})", DIM))
    print()
    print(f"  {c('dir:', DIM)} {run_dir}")
    print(f"  {c('tip:', DIM)} {c(f'/open {run_dir.name}', CYN)} to reveal in Finder")


def _render_step_line(s: dict) -> None:
    name = s.get("action", "?")
    bullet = c("●", CYN)
    bits = [name]
    if s.get("x") is not None:    bits.append(f"({s['x']},{s['y']})")
    if s.get("text"):             bits.append(f"text={_shorten(s['text'], 40)!r}")
    if s.get("key"):              bits.append(f"key={s['key']}")
    if s.get("reason"):           bits.append(f"reason={_shorten(s['reason'], 60)!r}")
    elapsed = s.get("elapsed_ms")
    elapsed_s = c(f"  ({elapsed}ms)", DIM) if elapsed else ""
    print(f"  {bullet} {' '.join(bits)}{elapsed_s}")
    res = s.get("result") or ""
    if res and not res.startswith("task ended"):
        print(f"     {DIM}└{RESET} {c(_shorten(res, 100), DIM)}")


def _cmd_open(arg: str | None) -> None:
    run_dir = _resolve_run(arg)
    if run_dir is None:
        print(c(f"no run matching {arg!r}", RED))
        return
    import subprocess
    subprocess.run(["open", "-R", str(run_dir / "task.json")], check=False)
    print(f"  revealed {run_dir} in Finder")


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

@dataclass
class TaskSummary:
    """Tiny per-task summary for session memory. Held in REPL state and
    prepended to next task's prompt so the model has context across
    tasks WITHOUT replaying full screenshot history."""
    task: str
    status: str            # "done" | "fail" | "verify_failed" | "cap"
    result: str            # last action's reason, truncated
    trace_id: str | None   # for /show
    n_steps: int


def _build_session_context(history: list["TaskSummary"], variables: dict) -> str:
    """Format session memory as a short prompt prefix for the next task.

    Bounded — keep last 3 tasks + variables. Avoids the Cua-style
    long-transcript explosion: we ONLY include {task, status, result},
    no screenshots, no per-step trace.
    """
    if not history and not variables:
        return ""
    lines = ["RECENT SESSION CONTEXT (read-only, for reference):"]
    for s in history[-3:]:
        lines.append(f'  - "{s.task}" → {s.status}: {_shorten(s.result, 100)}')
    if variables:
        var_str = ", ".join(f"{k}={v!r}" for k, v in variables.items())
        lines.append(f"  variables: {var_str}")
    lines.append("END SESSION CONTEXT")
    return "\n".join(lines)


def _run_task(task: str, opts: dict,
              session: list["TaskSummary"], variables: dict) -> None:
    # one-line header, dimmed
    flags = []
    if opts["dry_run"]:    flags.append(c("dry", YEL))
    if opts["confirm_each"]: flags.append(c("confirm", YEL))
    if opts["max_steps"] != 20: flags.append(f"steps≤{opts['max_steps']}")
    flag_str = ("  " + " ".join(flags)) if flags else ""
    print(f"  {c('▸', BLU)} {c(task, BOLD)}{flag_str}")
    print()

    # Pass session context separately so trace files record the user's
    # clean task (not the augmented one) — fixes a /history mismatch
    # where the displayed task was the prefixed context block.
    ctx_block = _build_session_context(session, variables)

    t0 = time.time()
    try:
        history = run(
            task,
            max_steps=opts["max_steps"],
            dry_run=opts["dry_run"],
            confirm_each=opts["confirm_each"],
            sleep_between=0.0,
            callbacks=_build_repl_callbacks(),
            session_context=ctx_block,
            quiet=True,
        )
    except KeyboardInterrupt:
        print(c("\n  ⏵ interrupted", YEL))
        # Still record the attempt so the next task's session context
        # reflects what the user was last working on. Without this, a
        # follow-up like "试试不用bash再来" loses its referent and the
        # model anchors to the previous successful task instead.
        session.append(TaskSummary(
            task=task, status="interrupted",
            result="user interrupted before completion",
            trace_id=None, n_steps=0,
        ))
        return
    except Exception as e:
        print(c(f"\n  ✗ {e}", RED))
        session.append(TaskSummary(
            task=task, status="error",
            result=f"runtime error: {e}",
            trace_id=None, n_steps=0,
        ))
        return

    secs = time.time() - t0
    n = len(history)
    last = history[-1] if history else None
    status = last.action.name if last else "?"

    # Locate the run directory via the trace_id symlink we just updated.
    out_dir = None
    latest = _RUNS_ROOT / "latest"
    if latest.is_symlink():
        try:
            out_dir = latest.resolve()
        except Exception:
            out_dir = None

    in_tok = sum((s.usage or {}).get("input_tokens", 0)  for s in history)
    out_tok = sum((s.usage or {}).get("output_tokens", 0) for s in history)
    cost = in_tok / 1e6 * 0.50 + out_tok / 1e6 * 4.00

    print()
    # Prefer the canonical status from final.json (it correctly handles
    # crashed runs as "failed", which deriving from history alone cannot).
    canonical_status = "cap"
    if out_dir is not None:
        final_p = out_dir / "final.json"
        if final_p.exists():
            try:
                canonical_status = (json.loads(final_p.read_text()).get("status")
                                    or "cap").lower()
            except Exception:
                pass
    if canonical_status == "cap":   # fall back to the action-derived view
        term_status = (last.action.status or "done").lower() if last and last.action.name == "terminate" else None
        if status == "done" or (status == "terminate" and term_status == "done"):
            canonical_status = "done"
        elif status in ("fail", "verify_failed") or (status == "terminate" and term_status == "fail"):
            canonical_status = term_status if status == "terminate" else status

    if canonical_status == "done":
        head = c("✓ done", GRN, BOLD)
    elif canonical_status == "failed":
        head = c("✗ failed", RED, BOLD)
    elif canonical_status in ("fail", "verify_failed"):
        head = c("⚠ " + canonical_status, YEL, BOLD)
    else:
        head = c("• cap", DIM, BOLD)
    summary = (f"  {head}  {n} step{'s' if n != 1 else ''} · {secs:.1f}s · "
               f"{c(f'{in_tok:,} in / {out_tok:,} out', DIM)} · "
               f"{c(f'~${cost:.3f}', DIM)}")
    print(summary)
    result_text = last.action.reason if (last and last.action.reason) else ""
    if result_text:
        # Print the full reason, wrapped at 90 chars so terminal users see
        # everything. Long news/research summaries shouldn't be truncated to
        # one line — the user just spent tokens to produce them.
        import textwrap
        body = textwrap.fill(result_text, width=90,
                             initial_indent="        → ",
                             subsequent_indent="          ")
        print(c(body, DIM))
    if out_dir:
        print(c(f"        ↳ {out_dir.name}", DIM))

    # Append to session memory + update $last
    session.append(TaskSummary(
        task=task,
        status=canonical_status,
        result=result_text,
        trace_id=(out_dir.name if out_dir else None),
        n_steps=n,
    ))
    if result_text:
        # Bounded — long termination reasons (e.g. document summaries)
        # would otherwise inflate every subsequent prompt indefinitely.
        variables["last"] = _shorten(result_text, 200)


def _cmd_context(session: list["TaskSummary"], variables: dict) -> None:
    if not session and not variables:
        print(c("(empty session — model will see no prior context)", DIM))
        return
    print(c("Session context the model sees on the next task:", BOLD))
    print()
    block = _build_session_context(session, variables)
    if block:
        for line in block.splitlines():
            print(f"  {c(line, DIM)}")
    else:
        print(c("  (none)", DIM))


def _cmd_reset(session: list["TaskSummary"], variables: dict) -> None:
    n = len(session)
    session.clear()
    variables.clear()
    print(c(f"  cleared {n} task(s) from session memory", DIM))


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

    # Session memory: bounded summary of recent tasks + variable bag.
    # Cleared by /reset; passed to _run_task so each next task sees a
    # short context block prefixed to the user's request.
    session: list[TaskSummary] = []
    variables: dict[str, str] = {}

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
                parts = line.split()
                cmd = parts[0].lower()
                arg = parts[1] if len(parts) > 1 else None
                if cmd in ("/exit", "/quit"):
                    break
                if cmd == "/help":
                    _cmd_help()
                elif cmd == "/status":
                    _cmd_status()
                elif cmd == "/history":
                    n = int(arg) if arg and arg.isdigit() else 10
                    _cmd_history(n)
                elif cmd == "/show":
                    _cmd_show(arg)
                elif cmd == "/open":
                    _cmd_open(arg)
                elif cmd == "/context":
                    _cmd_context(session, variables)
                elif cmd == "/reset":
                    _cmd_reset(session, variables)
                elif cmd == "/clear":
                    _cmd_clear()
                else:
                    print(c(f"unknown command: {cmd}", RED))
                continue

            task, opts = _parse_task_flags(line)
            if not task:
                continue
            _run_task(task, opts, session, variables)

    finally:
        _save_history()

    print(c("bye.", DIM))
    return 0
