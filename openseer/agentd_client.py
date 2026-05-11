"""Sync WebSocket client for `openseer agentd`.

Used by:
- `openseer task` CLI — attach to a running daemon when one exists,
  so manual `openseer task "..."` invocations share state with the
  GUI session and skip the cold Python import every run. Falls back
  cleanly to direct `agent.run()` if no daemon is reachable.

- (future) the Telegram daemon — route inbound chat messages through
  the same agentd instance the GUI uses, so all clients see the
  same ask_user / hand-off / event stream surface.

If the daemon isn't running, `try_open()` returns None and the
caller is expected to fall back to direct in-process execution.
"""
from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator, Optional

import websockets.exceptions
from websockets.sync.client import connect


_RENDEZVOUS_PATH = Path.home() / ".openseer" / "agentd.json"


@dataclass
class _Pending:
    """One in-flight server-initiated round-trip (currently only
    ask_user) — we send a reply once stdin produces an answer."""
    request_id: str
    payload: dict[str, Any]


class AgentdClient:
    """Sync agentd client. Construct with `try_open()` (which connects +
    auths) and call `close()` when done. Don't use `with` —
    `try_open` already entered the connection."""

    def __init__(self, host: str, port: int, token: str):
        self.url = f"ws://{host}:{port}"
        self.token = token
        self.ws: Any = None
        self._rid = 0

    def connect_and_auth(self) -> None:
        """Called once by try_open(). Raises on failure; caller
        catches and falls back to the direct path."""
        self.ws = connect(self.url, open_timeout=3.0)
        rid = self._send({"type": "auth", "token": self.token})
        ack = self._recv()
        if ack.get("type") != "ack" or ack.get("request_id") != rid:
            raise ConnectionError(f"agentd auth failed: {ack}")

    def close(self) -> None:
        if self.ws is not None:
            try:
                self.ws.close()
            except Exception:
                pass
            self.ws = None

    # ─── low-level ──────────────────────────────────────────────────

    def _next_rid(self) -> str:
        self._rid += 1
        return f"r{self._rid}"

    def _send(self, msg: dict[str, Any]) -> str:
        rid = self._next_rid()
        out = {**msg, "request_id": rid}
        self.ws.send(json.dumps(out, ensure_ascii=False))
        return rid

    def _recv(self) -> dict[str, Any]:
        raw = self.ws.recv()
        return json.loads(raw)

    # ─── high-level ─────────────────────────────────────────────────

    def run_task(self, task: str, *, dry_run: bool,
                 max_steps: int = 200,
                 session_context: str = "",
                 ask_user_via_stdin: bool = True
                 ) -> Iterator[dict[str, Any]]:
        """Send start_task, yield each event dict as it arrives.

        Yields a synthetic `{"type": "_ack", "run_id": ...}` first.
        Continues until the agent emits task_finished or task_failed,
        then returns.

        If the agent calls ask_user, this generator interactively
        prompts on stdin and posts user_reply automatically. Set
        `ask_user_via_stdin=False` to instead reply None (timeout
        path) — useful in scripts where there's no human.
        """
        msg: dict[str, Any] = {
            "type": "start_task",
            "task": task,
            "dry_run": dry_run,
            "max_steps": max_steps,
        }
        if session_context:
            msg["session_context"] = session_context
        sent_rid = self._send(msg)

        run_id: Optional[str] = None

        while True:
            reply = self._recv()
            t = reply.get("type")

            # Start-task ack (must match our request_id).
            if (t == "ack" and reply.get("request_id") == sent_rid
                    and "run_id" in reply):
                run_id = reply["run_id"]
                yield {"type": "_ack", "run_id": run_id}
                continue

            # Server-initiated ask_user — prompt stdin synchronously
            # and post user_reply back.
            if t == "ask_user":
                self._handle_ask_user(reply, via_stdin=ask_user_via_stdin)
                continue

            # Events for our run.
            if t == "event" and reply.get("run_id") == run_id:
                ev = reply.get("event") or {}
                yield ev
                et = ev.get("type")
                if et in ("task_finished", "task_failed"):
                    return

            # Anything else (ack for our user_reply, errors) — ignore.

    def _handle_ask_user(self, msg: dict[str, Any], *,
                         via_stdin: bool) -> None:
        ask_id = msg.get("request_id") or ""
        question = msg.get("question") or ""
        kind = msg.get("kind") or "text"
        options = msg.get("options") or []

        reply: Optional[str]
        if not via_stdin:
            reply = None
        else:
            reply = _prompt_user_for(question, kind, options)

        out: dict[str, Any] = {"type": "user_reply", "ask_id": ask_id}
        out["reply"] = reply if reply is not None else None
        # Don't await ack (we'd block the event stream). Daemon's
        # ack flows back into the main loop and is ignored there.
        self._send(out)


def _prompt_user_for(question: str, kind: str,
                     options: list[str]) -> Optional[str]:
    """Block on stdin for a reply. Returns the chosen / typed
    string, or None if the user pressed Ctrl+D or just enter."""
    print()
    print(f"  ╔═ OpenSeer is asking ═════════════════════════")
    for line in question.splitlines() or [question]:
        print(f"  ║ {line}")
    if kind == "confirm":
        print("  ║ (y/n)")
    elif kind == "choose" and options:
        for i, opt in enumerate(options, 1):
            print(f"  ║  {i}) {opt}")
    print("  ╚══════════════════════════════════════════════")
    try:
        raw = input("  reply > ").strip()
    except EOFError:
        return None
    if not raw:
        return None
    if kind == "confirm":
        return ("Yes" if raw.lower() in ("y", "yes", "是", "ok")
                else "No")
    if kind == "choose" and options:
        if raw.isdigit() and 1 <= int(raw) <= len(options):
            return options[int(raw) - 1]
        # Allow free-text match too.
        for opt in options:
            if opt.lower() == raw.lower():
                return opt
    return raw


def load_rendezvous() -> dict[str, Any]:
    """Read ~/.openseer/agentd.json. Raises FileNotFoundError if
    the daemon hasn't written one (i.e., isn't running)."""
    if not _RENDEZVOUS_PATH.exists():
        raise FileNotFoundError(str(_RENDEZVOUS_PATH))
    return json.loads(_RENDEZVOUS_PATH.read_text())


def try_open(timeout: float = 2.0) -> Optional[AgentdClient]:
    """Best-effort: connect + auth an AgentdClient. Returns a ready-
    to-use client on success, None on any failure (no rendezvous,
    daemon dead, auth fail). Caller MUST call `close()` when done —
    don't `with` it, the connection is already established here.
    Falls back to direct agent.run() if None."""
    try:
        cfg = load_rendezvous()
        client = AgentdClient(cfg["host"], cfg["port"], cfg["token"])
        client.connect_and_auth()
        return client
    except (FileNotFoundError, KeyError, OSError, TimeoutError,
            ConnectionError, ConnectionRefusedError,
            websockets.exceptions.WebSocketException,
            json.JSONDecodeError):
        return None


# ─── rendering helpers (used by the CLI) ────────────────────────────


def render_event_to_stdout(ev: dict[str, Any]) -> None:
    """Translate an event into a terse human-readable line. Matches
    the spirit of agent.run()'s `say()` calls — not byte-identical,
    but enough that running `openseer task` against a live daemon
    feels familiar to someone used to the direct path."""
    et = ev.get("type", "?")
    data = ev.get("data") or {}
    step = ev.get("step")
    if et == "task_started":
        task = data.get("task", "")
        trace = data.get("trace_id", "")
        dry = " (dry_run)" if data.get("dry_run") else ""
        print(f"[agent] task: {task}{dry}")
        print(f"[agent] trace: {trace}")
    elif et == "step_started":
        print(f"\n──── step {step} ────")
    elif et == "model_started":
        pass  # noisy; skip
    elif et == "model_finished":
        usage = data.get("usage") or {}
        elapsed = data.get("elapsed_ms") or 0
        if usage:
            it = usage.get("input_tokens", 0)
            ot = usage.get("output_tokens", 0)
            print(f"  model: {it}+{ot} tokens · {elapsed}ms")
    elif et == "action_started":
        name = data.get("action") or "?"
        print(f"  → {name}")
    elif et == "action_finished":
        result = (data.get("result") or "")[:120]
        if result:
            print(f"    {result}")
    elif et == "step_recorded":
        pass  # already summarised
    elif et == "task_finished":
        status = data.get("status", "done")
        reason = (data.get("reason") or "")[:400]
        print(f"\n[agent] terminated: {status}")
        if reason:
            print(f"        {reason}")
    elif et == "task_failed":
        err = (data.get("error") or "")[:400]
        print(f"\n[agent] crashed: {err}")
    elif et == "agent_held":
        print(f"\n[agent] paused — touch ~/.openseer/runs/<trace>/HOLD to remove and resume")
    elif et == "agent_resumed":
        print(f"[agent] resumed")
