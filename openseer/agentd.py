"""WebSocket daemon — one process, many clients.

The GUI, voice orb, telegram bridge, and `openseer task` CLI all talk
to a single long-running `openseer agentd` over WebSocket. This
replaces the older "every client spawns its own `openseer task`
subprocess and tails events.jsonl" model and is the foundation for
features that need shared state across clients (parallel execution,
demonstration-recorded macros, agent-stuck "ask user" prompts).

This file is the Phase 1 skeleton: token-authed handshake, ping/pong,
and a stubbed `start_task` that emits a couple of synthetic events so
clients can wire the event stream end-to-end. The real agent runner
(currently in `openseer.agent`) is plugged in in Phase 2.

Rendezvous: clients read `~/.openseer/agentd.json` for { host, port,
token }. First message on the socket MUST be `{type:"auth", token}`
or the server drops the connection. The token is regenerated each
run.

Protocol (v1, JSON over WebSocket):

  client → server
    {type:"auth", token, request_id?}
    {type:"ping", request_id, payload?}
    {type:"start_task", request_id, task, dry_run?, session_context?}
    {type:"cancel_task", request_id, run_id}

  server → client
    {type:"ack", request_id, ...result}
    {type:"error", request_id?, error}
    {type:"pong", request_id, server_time, echo}
    {type:"event", run_id, event:{type, step, data}}     # shape mirrors events.jsonl

Run with:  openseer agentd
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import secrets
import signal
import sys
import time
import traceback
import uuid
from pathlib import Path
from typing import Any

import websockets

from .callbacks.base import Callback
from .events import TaskEvent


_DIR = Path.home() / ".openseer"
RENDEZVOUS_PATH = _DIR / "agentd.json"
PIDFILE_PATH = _DIR / "agentd.pid"
PROTOCOL_VERSION = 1

log = logging.getLogger("openseer.agentd")


# ─── pidfile / rendezvous ───────────────────────────────────────────


def _ensure_dir() -> None:
    _DIR.mkdir(parents=True, exist_ok=True)


def _is_pid_running(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _acquire_pidfile() -> bool:
    """Return True iff this process should own the daemon. False
    means another live `openseer agentd` is already on this account."""
    _ensure_dir()
    if PIDFILE_PATH.exists():
        try:
            existing = int(PIDFILE_PATH.read_text().strip())
        except Exception:
            existing = 0
        if _is_pid_running(existing):
            return False
        # stale pidfile from a crashed daemon — overwrite below
    PIDFILE_PATH.write_text(str(os.getpid()))
    return True


def _release_pidfile() -> None:
    try:
        if PIDFILE_PATH.exists() \
                and PIDFILE_PATH.read_text().strip() == str(os.getpid()):
            PIDFILE_PATH.unlink()
    except Exception:
        pass


def _write_rendezvous(port: int, token: str) -> None:
    payload = {
        "host": "127.0.0.1",
        "port": port,
        "token": token,
        "pid": os.getpid(),
        "started_at": time.time(),
        "protocol_version": PROTOCOL_VERSION,
    }
    tmp = RENDEZVOUS_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, indent=2))
    # Restrict to user — the file carries an auth token. We don't
    # want another local process (or a stray `cat ~/.openseer/*`) to
    # cheaply impersonate the GUI.
    os.chmod(tmp, 0o600)
    tmp.replace(RENDEZVOUS_PATH)


def _clear_rendezvous() -> None:
    try:
        RENDEZVOUS_PATH.unlink()
    except FileNotFoundError:
        pass


# ─── agent → ws bridge ──────────────────────────────────────────────


class WsStreamCallback(Callback):
    """Forward every TaskEvent the agent loop emits to a WebSocket
    client, in the same JSON shape clients already consume from
    `events.jsonl`. The agent loop is synchronous (and runs in a
    worker thread via asyncio.to_thread), so we hop back to the
    asyncio loop with `run_coroutine_threadsafe`.

    Wraps each event as:
      {"type": "event", "run_id": <id>,
       "event": {"type": ..., "timestamp": ..., "step": ..., "data": ...}}
    """

    name = "ws-stream"

    def __init__(self, loop: asyncio.AbstractEventLoop,
                 send_async: Any, run_id: str) -> None:
        self.loop = loop
        # send_async is a coroutine function (msg: dict) -> Coroutine
        self.send_async = send_async
        self.run_id = run_id

    def on_event(self, ctx: dict[str, Any], event: TaskEvent) -> None:
        if event is None:
            return
        # Field name MUST be `ts` (not `timestamp`) — clients
        # consuming this stream decode it as the same `RunEvent`
        # they use to parse events.jsonl, and that schema expects
        # `ts`. Caught by codex review on 68b9bd6.
        msg = {
            "type": "event",
            "run_id": self.run_id,
            "event": {
                "type": event.type,
                "ts": event.timestamp,
                "step": event.step,
                "data": _jsonable(event.data),
            },
        }
        # The agent thread can't await directly. Schedule on the
        # loop; we don't .result() it because we don't want to
        # block the agent on slow clients.
        try:
            asyncio.run_coroutine_threadsafe(
                self.send_async(msg), self.loop)
        except RuntimeError:
            # loop already closed (daemon shutting down) — drop the
            # event, nothing useful to do.
            pass


def _jsonable(obj: Any) -> Any:
    """Best-effort coercion of event payloads to JSON-safe primitives.

    Most event data is already strings/ints/dicts of those, but
    callbacks occasionally stuff Path / dataclass / set / bytes in.
    """
    if obj is None or isinstance(obj, (str, int, float, bool)):
        return obj
    if isinstance(obj, (list, tuple, set)):
        return [_jsonable(x) for x in obj]
    if isinstance(obj, dict):
        return {str(k): _jsonable(v) for k, v in obj.items()}
    if isinstance(obj, Path):
        return str(obj)
    if isinstance(obj, bytes):
        try:
            return obj.decode("utf-8", "replace")
        except Exception:
            return repr(obj)
    # Dataclasses, models, etc.
    if hasattr(obj, "__dict__"):
        try:
            return _jsonable(vars(obj))
        except Exception:
            return repr(obj)
    return repr(obj)


# ─── per-connection handler ─────────────────────────────────────────


class _Connection:
    """Bookkeeping for one client. Auth state, in-flight tasks,
    and pending ask_user round-trips."""

    def __init__(self, ws: Any, server: "Server"):
        self.ws = ws
        self.server = server
        self.authed = False
        self.tasks: dict[str, asyncio.Task] = {}
        # request_id → future that resolves when the client posts a
        # matching `user_reply`. Used by WsStreamCallback.ask_user
        # to bridge from the synchronous agent thread to the async
        # ws round-trip.
        self.ask_user_waiters: dict[str, asyncio.Future] = {}

    async def send(self, msg: dict[str, Any]) -> None:
        await self.ws.send(json.dumps(msg, ensure_ascii=False))

    async def run_ask_user(self, req_id: str, payload: dict[str, Any],
                           timeout: float = 300.0) -> Any:
        """Send an `ask_user` message to the client and await the
        matching `user_reply`. The sync `ask_user(...)` callable
        used by agent.run() bridges into this with
        `asyncio.run_coroutine_threadsafe`. Returns the user's reply
        (string for kind=text/choose, str for kind=confirm "Yes"/"No",
        or None on timeout / client disconnect)."""
        loop = asyncio.get_running_loop()
        fut: asyncio.Future = loop.create_future()
        self.ask_user_waiters[req_id] = fut
        try:
            await self.send(payload)
            return await asyncio.wait_for(fut, timeout=timeout)
        except (asyncio.TimeoutError, asyncio.CancelledError,
                websockets.exceptions.ConnectionClosed):
            return None
        finally:
            self.ask_user_waiters.pop(req_id, None)

    async def run(self) -> None:
        try:
            async for raw in self.ws:
                try:
                    msg = json.loads(raw)
                except json.JSONDecodeError as e:
                    await self.send({
                        "type": "error",
                        "error": f"bad json: {e}",
                    })
                    continue
                await self._dispatch(msg)
        except websockets.exceptions.ConnectionClosed:
            pass
        finally:
            # Cancel any in-flight tasks tied to this client.
            for t in self.tasks.values():
                t.cancel()
            # Resolve any pending ask_user waiters with None so the
            # synchronous agent thread doesn't block waiting on a
            # client that's gone (it would otherwise sit there for
            # the full 300s timeout). Agent treats None as a
            # no-reply and ends the task cleanly.
            for fut in list(self.ask_user_waiters.values()):
                if not fut.done():
                    fut.set_result(None)
            self.ask_user_waiters.clear()

    async def _dispatch(self, msg: dict[str, Any]) -> None:
        t = msg.get("type")
        rid = msg.get("request_id")

        if not self.authed:
            if t != "auth":
                await self.send({
                    "type": "error",
                    "request_id": rid,
                    "error": "auth required as first message",
                })
                await self.ws.close(code=4001, reason="unauthed")
                return
            if msg.get("token") != self.server.token:
                await self.send({
                    "type": "error",
                    "request_id": rid,
                    "error": "bad token",
                })
                await self.ws.close(code=4003, reason="bad token")
                return
            self.authed = True
            await self.send({
                "type": "ack",
                "request_id": rid,
                "protocol_version": PROTOCOL_VERSION,
            })
            return

        if t == "ping":
            await self.send({
                "type": "pong",
                "request_id": rid,
                "server_time": time.time(),
                "echo": msg.get("payload"),
            })
            return

        if t == "start_task":
            # Generate the run_id (= agent's trace_id) here so we can
            # ack the client BEFORE agent.run() starts producing
            # events. The agent writes its `events.jsonl` under
            # ~/.openseer/runs/<run_id>/, and cancel_task writes a
            # CANCEL sentinel into that same dir — same convention
            # the legacy subprocess path uses, so trace replay works
            # uniformly.
            run_id = uuid.uuid4().hex[:8]
            await self.send({
                "type": "ack",
                "request_id": rid,
                "run_id": run_id,
            })
            self.tasks[run_id] = asyncio.create_task(
                self._run_agent(
                    run_id,
                    task_text=msg.get("task", ""),
                    dry_run=bool(msg.get("dry_run", False)),
                    session_context=msg.get("session_context") or "",
                    max_steps=int(msg.get("max_steps") or 200),
                ))
            return

        if t == "user_reply":
            # Reply to a server-initiated ask_user. The original
            # ask_user's request_id is carried in `ask_id` here, not
            # in `request_id` — the latter belongs to the
            # user_reply's own ack flow, so we don't collide.
            # Reply may be a string (text/choose), a "Yes"/"No"
            # string for confirm, or null (user dismissed → agent
            # treats as no-reply timeout).
            ask = msg.get("ask_id")
            fut = self.ask_user_waiters.pop(ask, None) if ask else None
            if fut is not None and not fut.done():
                fut.set_result(msg.get("reply"))
            await self.send({
                "type": "ack", "request_id": rid,
                "delivered": ask,
            })
            return

        if t == "cancel_task":
            target = msg.get("run_id")
            # Two-pronged stop:
            #   1. Drop the CANCEL sentinel — the agent's outer loop
            #      checks for it before every step and exits with a
            #      synthetic `terminate(status="interrupted")`.
            #   2. Cancel the asyncio task too, in case the agent is
            #      currently mid-API-call (LLM stream) and would
            #      otherwise keep running until the request returns.
            #      Note: pyautogui actions in flight won't be killed
            #      by Task.cancel since they're inside to_thread —
            #      the sentinel-on-outer-loop pathway is what
            #      actually stops the work.
            if target:
                try:
                    sentinel = (Path.home() / ".openseer" / "runs"
                                / target / "CANCEL")
                    sentinel.parent.mkdir(parents=True, exist_ok=True)
                    sentinel.write_text("cancelled via agentd\n")
                except Exception as e:
                    log.warning("cancel: couldn't write sentinel: %s", e)
                task = self.tasks.pop(target, None)
                if task is not None:
                    task.cancel()
            await self.send({
                "type": "ack",
                "request_id": rid,
                "cancelled": target,
            })
            return

        await self.send({
            "type": "error",
            "request_id": rid,
            "error": f"unknown type: {t}",
        })

    async def _run_agent(self, run_id: str, *,
                         task_text: str,
                         dry_run: bool,
                         session_context: str,
                         max_steps: int) -> None:
        """Run the real `agent.run()` in a worker thread and stream
        every TaskEvent it emits back over this WebSocket via
        WsStreamCallback. The default callback set (trajectory /
        budget / safety / etc.) still runs alongside, so traces
        still land in events.jsonl on disk for replay."""
        # Import lazily — agent pulls in pyautogui, PyObjC, Pillow,
        # the OAuth client, …; we don't want to drag that in at
        # daemon startup when no task has been requested yet.
        from .agent import run as agent_run, _default_callbacks

        out_dir = Path.home() / ".openseer" / "runs" / run_id
        out_dir.mkdir(parents=True, exist_ok=True)

        loop = asyncio.get_running_loop()
        ws_cb = WsStreamCallback(loop, self.send, run_id)
        # Reuse the same default set the CLI uses (trajectory /
        # safety / budget / reflection / image-retention) so the on-
        # disk trace + safety net behave identically; the ws stream
        # is an additional observer.
        cbs = _default_callbacks(quiet=True) + [ws_cb]

        # ask_user bridge: synchronous call (from the agent worker
        # thread) → schedule coroutine on this asyncio loop →
        # round-trip with the connected client → return the user's
        # reply string (or None on timeout / disconnect).
        conn = self

        def ask_user_cb(question: str, kind: str,
                        options: Any = None,
                        attachments: Any = None) -> Any:
            req_id = "ask-" + uuid.uuid4().hex[:8]
            payload = {
                "type": "ask_user",
                "run_id": run_id,
                "request_id": req_id,
                "question": question,
                "kind": kind,
                "options": list(options or []),
                "attachments": list(attachments or []),
            }
            try:
                fut = asyncio.run_coroutine_threadsafe(
                    conn.run_ask_user(req_id, payload), loop)
            except RuntimeError:
                # loop already shut down — agent will get None and
                # convert to terminate(fail) per its own fallback.
                return None
            try:
                # 5 minutes is enough for a human, short enough that
                # an abandoned dialog eventually unblocks the agent.
                return fut.result(timeout=300)
            except Exception:
                fut.cancel()
                return None

        def _runner() -> None:
            agent_run(
                task_text,
                max_steps=max_steps,
                dry_run=dry_run,
                out_dir=out_dir,
                session_context=session_context,
                callbacks=cbs,
                ask_user=ask_user_cb,
                quiet=True,
            )

        try:
            await asyncio.to_thread(_runner)
        except asyncio.CancelledError:
            # The CANCEL sentinel path already produced a
            # task_finished(status="interrupted") via the agent
            # loop's normal flow. Nothing extra to do here other
            # than not re-raising as a crash.
            log.info("run %s cancelled via Task.cancel", run_id)
        except Exception as e:
            tb = traceback.format_exc(limit=8)
            log.exception("run %s crashed", run_id)
            # Surface as task_failed so the client doesn't hang
            # waiting on a terminal event. Mirrors the event the
            # agent itself would have emitted if it caught the
            # exception internally.
            await self.send({
                "type": "event",
                "run_id": run_id,
                "event": {
                    "type": "task_failed",
                    "ts": time.time(),
                    "step": None,
                    "data": {"error": str(e), "traceback": tb},
                },
            })
        finally:
            self.tasks.pop(run_id, None)


# ─── server ────────────────────────────────────────────────────────


class Server:
    def __init__(self) -> None:
        self.token = secrets.token_hex(32)
        self.port = 0

    async def handle(self, ws: Any) -> None:
        conn = _Connection(ws, self)
        log.info("client connected: %s", ws.remote_address)
        try:
            await conn.run()
        finally:
            log.info("client disconnected: %s", ws.remote_address)

    async def serve(self) -> None:
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        try:
            loop.add_signal_handler(signal.SIGINT, stop.set)
            loop.add_signal_handler(signal.SIGTERM, stop.set)
        except NotImplementedError:
            # not all platforms — fine, we'll rely on KeyboardInterrupt
            pass

        async with websockets.serve(
            self.handle, "127.0.0.1", 0,
            # 64KB ought to be enough for our messages for now; bump
            # later if we ever need to ship images inline (probably
            # better to send them as file refs).
            max_size=64 * 1024,
        ) as ws_server:
            socks = list(ws_server.sockets or [])
            if not socks:
                raise RuntimeError("agentd: no socket bound")
            self.port = socks[0].getsockname()[1]
            _write_rendezvous(self.port, self.token)
            log.info("agentd listening on 127.0.0.1:%d (pid=%d, "
                     "rendezvous=%s)",
                     self.port, os.getpid(), RENDEZVOUS_PATH)
            await stop.wait()
            log.info("agentd shutting down")


# ─── CLI entry point ────────────────────────────────────────────────


def run_agentd() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [agentd] %(message)s",
        stream=sys.stderr,
    )
    if not _acquire_pidfile():
        log.error("another agentd is already running (see %s). "
                  "Stop it first, or delete the pidfile if it's stale.",
                  PIDFILE_PATH)
        return 1
    try:
        asyncio.run(Server().serve())
    except KeyboardInterrupt:
        pass
    except Exception:
        log.exception("agentd crashed")
        return 2
    finally:
        _clear_rendezvous()
        _release_pidfile()
    return 0
