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
import uuid
from pathlib import Path
from typing import Any

import websockets


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


# ─── per-connection handler ─────────────────────────────────────────


class _Connection:
    """Bookkeeping for one client. Auth state, in-flight stub tasks."""

    def __init__(self, ws: Any, server: "Server"):
        self.ws = ws
        self.server = server
        self.authed = False
        self.tasks: dict[str, asyncio.Task] = {}

    async def send(self, msg: dict[str, Any]) -> None:
        await self.ws.send(json.dumps(msg, ensure_ascii=False))

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
            # Cancel any in-flight stub tasks tied to this client.
            for t in self.tasks.values():
                t.cancel()

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
            # Phase 1 stub: synthesize a tiny event stream so the
            # client wiring can be validated end-to-end. Phase 2 will
            # replace this with a real agent.run() invocation.
            run_id = "stub-" + uuid.uuid4().hex[:8]
            await self.send({
                "type": "ack",
                "request_id": rid,
                "run_id": run_id,
            })
            self.tasks[run_id] = asyncio.create_task(
                self._run_stub(run_id, msg.get("task", "")))
            return

        if t == "cancel_task":
            target = msg.get("run_id")
            task = self.tasks.pop(target, None) if target else None
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

    async def _run_stub(self, run_id: str, task: str) -> None:
        await self.send({
            "type": "event",
            "run_id": run_id,
            "event": {
                "type": "task_started",
                "step": 0,
                "data": {"task": task, "trace_id": run_id},
            },
        })
        try:
            await asyncio.sleep(0.3)
        except asyncio.CancelledError:
            await self.send({
                "type": "event",
                "run_id": run_id,
                "event": {
                    "type": "task_finished",
                    "step": 1,
                    "data": {"status": "interrupted"},
                },
            })
            return
        await self.send({
            "type": "event",
            "run_id": run_id,
            "event": {
                "type": "task_finished",
                "step": 1,
                "data": {"status": "done", "reason": "(stub)"},
            },
        })


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
