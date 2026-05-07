"""Long-running daemon mode: `openseer daemon`.

Listens on configured inbound channels (Telegram for now), turns each
message into a task, runs it through the agent loop, replies with the
result. One task at a time per channel.

Multi-turn: each chat_id has its own bounded session memory persisted
to ``~/.openseer/inbox/sessions.json``, so a follow-up message
("now post the screenshot") sees the prior task as context.

Live progress: the daemon edits the original ack message as the agent
makes progress (each step's thought + last action), so the phone-side
sees what's happening rather than a 30-second silence.

Channel configuration in ``~/.openseer/config.json``::

    {
      "provider": "anthropic",
      "telegram": {
        "enabled": true,
        "token": "123:abc...",
        "allowed_chat_ids": [123456789],
        "trigger_prefix": "openseer:",
        "max_steps": 25,
        "confirm_each": false
      }
    }
"""
from __future__ import annotations

import json
import signal
import time
from pathlib import Path

from .agent import OAI_MODEL, run as agent_run
from .callbacks.base import Callback
from .events import EventType, TaskEvent
from .inbox.sessions import ChatSessions, TaskSummary
from .inbox.telegram import TelegramBot, TelegramMessage


_CONFIG_PATH = Path.home() / ".openseer" / "config.json"


def _load_config() -> dict:
    if not _CONFIG_PATH.exists():
        return {}
    try:
        return json.loads(_CONFIG_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


# ─── remote-mode prompt context ─────────────────────────────────────────────

_TERMINAL_KEYWORDS = (
    "iterm", "terminal", "warp", "tabby", "alacritty",
    "ghostty", "kitty", "wezterm", "hyper",
)


def _detect_host_terminal() -> tuple[str, int] | None:
    """Identify the terminal app the daemon was launched from.

    Strategy: walk our own process's parent chain until we find a process
    whose command name matches a known terminal emulator. This is far more
    reliable than NSWorkspace.frontmostApplication() (which depends on
    whatever app happens to have focus at daemon startup, including a
    Finder window the user just clicked into).

    Returns (app_localized_name, pid) on success, or None.
    """
    import os
    import subprocess

    pid = os.getppid()
    for _ in range(12):
        if pid <= 1:
            break
        try:
            r = subprocess.run(
                ["ps", "-o", "ppid=,comm=", "-p", str(pid)],
                capture_output=True, text=True, timeout=2,
            )
        except Exception:
            break
        line = (r.stdout or "").strip()
        if not line:
            break
        try:
            ppid_s, comm = line.split(None, 1)
            ppid = int(ppid_s)
        except ValueError:
            break
        base = comm.rsplit("/", 1)[-1].lower()
        if any(k in base for k in _TERMINAL_KEYWORDS):
            # Resolve a friendly localized name when possible (so the
            # prompt says 'iTerm2' not '/Applications/.../iTerm2').
            name = comm.rsplit("/", 1)[-1].split(".")[0] or comm
            try:
                from AppKit import NSRunningApplication  # type: ignore[import-untyped]
                ra = NSRunningApplication.runningApplicationWithProcessIdentifier_(pid)
                if ra is not None:
                    nm = ra.localizedName()
                    if nm:
                        name = str(nm)
            except Exception:
                pass
            return (name, pid)
        pid = ppid
    # Fallback: NSWorkspace's frontmost — but ONLY if it actually looks
    # like a terminal. If we mislabel a user's frontmost Safari/Finder/etc
    # as the "host terminal", the prompt will instruct the model to never
    # click into it, breaking subsequent tasks targeting that app. Better
    # to return None and let the model see the un-augmented notice.
    try:
        from AppKit import NSWorkspace  # type: ignore[import-untyped]
        front = NSWorkspace.sharedWorkspace().frontmostApplication()
        if front is not None:
            name = str(front.localizedName() or "")
            if any(k in name.lower() for k in _TERMINAL_KEYWORDS):
                return (name or "Terminal", int(front.processIdentifier()))
    except Exception:
        pass
    return None


def _build_remote_notice(host_term: tuple[str, int] | None) -> str:
    host_line = ""
    if host_term:
        name, pid = host_term
        host_line = (
            f"  - The terminal hosting this daemon is {name!r} (pid={pid}). "
            f"Its AX tree will be flagged in the on-screen-elements block "
            f"with a [NOTE] line. Don't act on the daemon's own log output. "
            f"To work on a different app call "
            f"`get_app_state app=\"<target>\"` to bring it forward and dump "
            f"its AX directly.\n"
        )
    return (
        "[OpenSeer is running in DAEMON mode, triggered remotely via "
        "Telegram chat. The user is NOT at this Mac right now — they only "
        "see what you send back. So:\n"
        f"{host_line}"
        "  - If the request is AMBIGUOUS with no referent in this chat's "
        "    prior tasks (e.g. 'do it again' with nothing in history), "
        "    terminate(fail) and ask for clarification — don't guess.\n"
        "  - DESCRIBE what's on screen in terminate.reason. To attach an "
        "    image, take a screenshot to a file and include "
        "    `\"attachments\":[\"/path.png\"]` on terminate "
        "    (PNG/JPG/GIF, ≤10 MB).\n"
        "  - VERIFY the actual end state before terminate(done) — no human "
        "    will catch a wrong claim.]"
    )


# Built once per daemon process at module-import is too early (NSWorkspace
# may not be initialised); we resolve in run_daemon() and cache here.
_REMOTE_NOTICE: str = _build_remote_notice(None)


# ─── live-progress callback ─────────────────────────────────────────────────

class _TelegramProgress(Callback):
    """Pushes step-level progress into the user's Telegram chat by
    editing the ack message in place. Throttled so we never exceed
    Telegram's rate limit (1 edit per second per chat is safe; we go
    every ~2.5s)."""

    name = "TelegramProgress"

    def __init__(self, bot: TelegramBot, chat_id: int, message_id: int,
                 task_text: str) -> None:
        self.bot = bot
        self.chat_id = chat_id
        self.message_id = message_id
        self.task_head = (task_text[:120] + "…") if len(task_text) > 120 else task_text
        self._last_edit = 0.0
        self._last_text = ""

    def _push(self, body: str) -> None:
        # Throttle: ≤ 1 edit per 2.5s, and skip if same text
        now = time.time()
        if now - self._last_edit < 2.5:
            return
        text = f"⏳ working on:\n{self.task_head}\n\n{body}"
        if text == self._last_text:
            return
        self._last_edit = now
        self._last_text = text
        try:
            self.bot.edit(self.chat_id, self.message_id, text)
        except Exception as e:
            print(f"  [telegram] progress edit failed: {e}")

    def on_event(self, ctx: dict, event: TaskEvent) -> None:
        if event.type == EventType.STEP_RECORDED:
            history = ctx.get("history") or []
            if not history:
                return
            s = history[-1]
            a = s.action
            # short action descriptor
            if a.name == "click":
                act = f"click ({a.x},{a.y})" + (f" ×{a.count}" if a.count > 1 else "")
            elif a.name == "type":
                act = f"type {(a.text or '')[:24]!r}"
            elif a.name == "key":
                act = f"key {a.key}"
            elif a.name == "open_app":
                act = f"open {a.app}"
            elif a.name == "bash":
                act = f"bash {(a.cmd or '')[:30]!r}"
            elif a.name in ("read_skill", "write_skill"):
                act = f"{a.name} {a.skill_name!r}"
            elif a.name in ("web_search",):
                act = f"web_search {(a.query or '')[:30]!r}"
            elif a.name == "web_fetch":
                act = f"web_fetch {(a.url or '')[:40]}"
            else:
                act = a.name
            thought = (a.thought or "").replace("\n", " ")
            if len(thought) > 120:
                thought = thought[:120] + "…"
            n = len(history)
            self._push(f"step {n} · {act}\n💭 {thought}")


# ─── dispatcher ─────────────────────────────────────────────────────────────


def _format_result(history: list, dur_s: float) -> str:
    last = history[-1] if history else None
    if not last:
        return f"⚠ no steps executed ({dur_s:.1f}s)"
    a = last.action
    if a.name == "terminate":
        st = (a.status or "done").lower()
        glyph = "✓" if st == "done" else "⚠"
        head = f"{glyph} {st}  {len(history)} steps · {dur_s:.1f}s"
        return f"{head}\n\n{a.reason or ''}" if a.reason else head
    if a.name in ("done", "fail"):
        glyph = "✓" if a.name == "done" else "⚠"
        return f"{glyph} {a.name}  {len(history)} steps · {dur_s:.1f}s\n\n{a.reason or ''}"
    return f"• stopped at step {len(history)} ({dur_s:.1f}s) — last: {a.name}"


def _canonical_status(history: list) -> tuple[str, str]:
    """Returns (status, result_text) for the session-memory record."""
    if not history:
        return "empty", ""
    last = history[-1]
    a = last.action
    result = a.reason or ""
    if a.name == "terminate":
        return (a.status or "done").lower(), result
    if a.name in ("done", "fail", "verify_failed"):
        return a.name, result
    return "cap", result


def _make_dispatcher(bot: TelegramBot, sessions: ChatSessions, *,
                     max_steps: int, confirm_each: bool):
    """Returns the on_message callback. Captures bot + session store."""

    def on_message(msg: TelegramMessage) -> None:
        print(f"\n[telegram] {msg.sender_name} ({msg.chat_id}) → {msg.text[:80]!r}")

        # Send ack so the user sees we got the message; we'll edit this
        # message in place with progress, and finally with the result.
        try:
            ack = bot.send(msg.chat_id,
                           f"⏳ working on:\n{msg.text[:200]}",
                           reply_to=msg.message_id)
            ack_msg_id = int(ack.get("message_id", 0))
        except Exception as e:
            print(f"  [telegram] ack failed: {e}")
            ack_msg_id = 0

        # Build session_context: remote-mode notice + prior tasks of this chat
        ctx_parts = [_REMOTE_NOTICE]
        prior_block = sessions.render_context(msg.chat_id)
        if prior_block:
            ctx_parts.append(prior_block)
        session_context = "\n\n".join(ctx_parts)

        # Live-progress callback (only if we got an ack message_id back).
        from .agent import _default_callbacks  # local import: callbacks
        callbacks = _default_callbacks(quiet=False)
        for cb in callbacks:
            if getattr(cb, "label", "") == "RunReflection":
                cb.mode = "trace-only"
        if ack_msg_id:
            callbacks.append(_TelegramProgress(bot, msg.chat_id, ack_msg_id,
                                               msg.text))

        t0 = time.time()
        try:
            history = agent_run(
                msg.text,
                max_steps=max_steps,
                dry_run=False,
                confirm_each=confirm_each,
                callbacks=callbacks,
                session_context=session_context,
                quiet=False,
            )
        except KeyboardInterrupt:
            try:
                if ack_msg_id:
                    bot.edit(msg.chat_id, ack_msg_id, "⏵ interrupted")
                else:
                    bot.send(msg.chat_id, "⏵ interrupted")
            except Exception:
                pass
            raise
        except Exception as e:
            print(f"  [agent] error: {e!r}")
            try:
                bot.send(msg.chat_id, f"✗ run errored: {e}", reply_to=msg.message_id)
            except Exception:
                pass
            return

        dur = time.time() - t0
        result = _format_result(history, dur)
        # Edit ack with summary; if reason is long, send the rest as
        # one or more chunked follow-up messages (Telegram caps at 4096
        # chars per message — long terminate.reason values would be
        # silently truncated otherwise).
        try:
            if ack_msg_id:
                short = result.split("\n\n", 1)[0]
                bot.edit(msg.chat_id, ack_msg_id, short)
                rest = result[len(short):].strip()
                if rest:
                    bot.send_long(msg.chat_id, rest, reply_to=ack_msg_id)
            else:
                bot.send_long(msg.chat_id, result, reply_to=msg.message_id)
        except Exception as e:
            print(f"  [telegram] reply send failed: {e}")

        # If the terminate action declared attachments, ship them too.
        # Lets the model send a screenshot back to the user's phone via
        # `{"action":"terminate","status":"done","reason":"...",
        #   "attachments":["/Users/.../proof.png"]}`. PNG/JPG/GIF only;
        # missing or oversized files just log a warning.
        last = history[-1] if history else None
        attaches: list[str] = []
        if last and last.action.name == "terminate":
            attaches = list(last.action.attachments or [])
        for path in attaches:
            try:
                if not Path(path).exists():
                    print(f"  [telegram] attachment missing: {path}")
                    continue
                bot.send_photo(msg.chat_id, path,
                               reply_to=ack_msg_id or msg.message_id)
            except Exception as e:
                print(f"  [telegram] sendPhoto failed for {path}: {e}")

        # Record into session memory for the next message in this chat
        status, result_text = _canonical_status(history)
        # Find trace_id from latest run (TrajectoryCallback wrote ~/.openseer/runs/<id>)
        trace_id: str | None = None
        try:
            latest = Path.home() / ".openseer" / "runs" / "latest"
            if latest.is_symlink():
                trace_id = latest.resolve().name
        except Exception:
            pass
        sessions.append(msg.chat_id, TaskSummary(
            task=msg.text,
            status=status,
            result=(result_text or "")[:300],
            trace_id=trace_id,
            ts=time.time(),
        ))
        print(f"[telegram] replied ({dur:.1f}s, {len(history)} steps, status={status})")

    return on_message


def run_daemon() -> int:
    # Detect the terminal app the daemon is launched from BEFORE we print
    # anything else — at this moment the user has just hit return in their
    # terminal, so frontmostApplication() is reliably the host terminal
    # itself (Terminal.app, iTerm2, Warp, …). We bake its name + pid into
    # the remote-mode prompt so the model knows exactly which app NOT to
    # drive when it sees [agent]/[step]/[telegram] log lines on screen.
    global _REMOTE_NOTICE
    host_term = _detect_host_terminal()
    _REMOTE_NOTICE = _build_remote_notice(host_term)
    # Tell the AX layer which pids belong to the daemon's host
    # terminal (GUI app pid + any session-helper pids in our parent
    # chain — iTermServer is parented by launchd, so we need both).
    # render_ax_for_prompt then flags the AX block with a [NOTE]
    # whenever it's dumping any of these. We don't block — sometimes
    # a task legitimately drives the terminal — we just annotate.
    from . import ax as _ax_mod
    _ax_mod.HOST_TERMINAL_PIDS = _ax_mod._terminal_app_pids_in_ancestry()

    cfg = _load_config()
    tg_cfg = cfg.get("telegram") or {}
    if not tg_cfg.get("enabled"):
        print("daemon: no inbound channel enabled in ~/.openseer/config.json.\n"
              "Add a `telegram` block with `enabled: true` and a bot token.\n"
              "See `openseer setup` for guidance.")
        return 1
    token = tg_cfg.get("token")
    if not token:
        print("daemon: telegram.token is missing in config. Get one from @BotFather.")
        return 1

    bot = TelegramBot(
        token=token,
        allowed_chat_ids=tg_cfg.get("allowed_chat_ids") or [],
        trigger_prefix=tg_cfg.get("trigger_prefix") or "",
        poll_timeout=int(tg_cfg.get("poll_timeout") or 30),
    )
    sessions = ChatSessions()

    try:
        me = bot.get_me()
    except Exception as e:
        print(f"daemon: telegram getMe failed — {e}")
        print("       check the token in ~/.openseer/config.json.")
        return 1

    print(f"daemon: telegram bot @{me.get('username')} ({me.get('first_name')}) ready")
    print(f"        provider={OAI_MODEL}")
    if host_term:
        print(f"        host terminal: {host_term[0]!r} (pid={host_term[1]}) — "
              f"prompt warns the model to never drive it")
    if bot.allowed_chat_ids:
        print(f"        allowed chat_ids: {sorted(bot.allowed_chat_ids)}")
    else:
        print(f"        ⚠ no allowed_chat_ids configured — daemon will REFUSE every "
              f"message and log the chat_id, so you can copy it into config. "
              f"Send a message from your phone, watch the log, then set "
              f"`telegram.allowed_chat_ids: [<id>]` in ~/.openseer/config.json.")
    if bot.trigger_prefix:
        print(f"        trigger prefix: {bot.trigger_prefix!r}")
    print(f"        sessions persisted to ~/.openseer/inbox/sessions.json")
    print("        (Ctrl+C to stop)")

    # Ctrl+C handling. The default SIGINT handler raises KeyboardInterrupt,
    # which agent_run() catches and the daemon's outer try also catches —
    # that's the clean exit path. Our previous "polite" handler just
    # printed and set bot._stop, which SWALLOWED SIGINT entirely (the
    # agent loop's blocking model call never saw KeyboardInterrupt and
    # the user had to keep mashing Ctrl+C with no effect).
    #
    # New behaviour:
    #   1st Ctrl+C: print, request bot stop, then raise KeyboardInterrupt
    #               so any in-flight agent run actually unwinds.
    #   2nd Ctrl+C: hard exit (the user was clearly serious).
    _caught = {"once": False}
    import os as _os

    def _on_sigint(signum, frame):
        if _caught["once"]:
            print("\ndaemon: hard exit (2nd Ctrl+C).")
            _os.kill(_os.getpid(), signal.SIGKILL)
        _caught["once"] = True
        print("\ndaemon: stop signal — interrupting current task …")
        bot.stop()
        raise KeyboardInterrupt
    signal.signal(signal.SIGINT, _on_sigint)
    signal.signal(signal.SIGTERM, _on_sigint)

    on_msg = _make_dispatcher(
        bot, sessions,
        max_steps=int(tg_cfg.get("max_steps") or 25),
        confirm_each=bool(tg_cfg.get("confirm_each", False)),
    )
    try:
        bot.poll(on_msg)
    except KeyboardInterrupt:
        pass
    print("daemon: stopped.")
    return 0
