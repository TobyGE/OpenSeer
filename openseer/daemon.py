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
import threading
import time
import uuid
from pathlib import Path

from .agent import OAI_MODEL, run as agent_run
from .callbacks.base import Callback
from .callbacks.run_reflection import extract_skill_block
from .events import EventType, TaskEvent
from .inbox.sessions import ChatSessions, TaskSummary
from .inbox.telegram import TelegramBot, TelegramCallback, TelegramMessage
from .skills import parse_skill_text, write_user_skill


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


def _latest_trace_id() -> str | None:
    """Return the newest trajectory id written by TrajectoryCallback."""
    try:
        latest = Path.home() / ".openseer" / "runs" / "latest"
        if latest.is_symlink():
            return latest.resolve().name
    except Exception:
        pass
    return None


def _trace_path_for_id(trace_id: str) -> Path | None:
    safe = trace_id.replace("-", "").replace("_", "")
    if not trace_id or not safe.isalnum():
        return None
    path = Path.home() / ".openseer" / "runs" / trace_id / "trace.md"
    return path if path.exists() else None


def _skill_body_from_trace(trace_id: str) -> str | None:
    path = _trace_path_for_id(trace_id)
    if path is None:
        return None
    try:
        return extract_skill_block(path.read_text(encoding="utf-8"))
    except Exception:
        return None


# ─── ask_user (model pauses to ask the user mid-run) ─────────────────────

# Same 5-min cap as step_check; conservative — give up rather than block
# the worker indefinitely waiting for someone who's away from their phone.
_ASK_USER_TIMEOUT_S = 300.0


class _AskController:
    """Worker-side handle for an active ask_user call.

    For ``kind="confirm"`` and ``kind="choose"`` the user replies via
    inline buttons; the callback handler decodes the option from
    callback_data and delivers the option text. For ``kind="text"``,
    the user's NEXT plain message in this chat is captured by the
    daemon's on_message router and delivered as the reply (so the
    user just types their answer like any other message).

    ``sender_id`` is the Telegram user id of the person who started
    the task. Replies (button click or text) are only honored when
    they come from this same sender. Without that bind, group-chat
    deployments would let any participant tap Continue / answer the
    text question, which can authorise hard-to-reverse actions (pay,
    send, post) on behalf of the wrong user.
    """

    def __init__(self, kind: str, options: list[str],
                 sender_id: int) -> None:
        self._event = threading.Event()
        self._reply: str | None = None
        self.kind = kind
        self.options = list(options or [])
        self.sender_id = int(sender_id)
        self.nonce: str = uuid.uuid4().hex[:12]

    def deliver(self, reply: str) -> None:
        self._reply = reply
        self._event.set()

    def wait(self, timeout: float) -> str | None:
        return self._reply if self._event.wait(timeout) else None


_active_ask_controllers: dict[int, _AskController] = {}


def _ask_button_markup(chat_id: int, nonce: str,
                       options: list[str]) -> dict:
    """Render an inline-keyboard with one button per option.

    Each button's callback_data is ``ask_btn:<chat>:<nonce>:<index>``
    so the handler can recover both which prompt this is for (nonce
    binding, same trick as step-check) and which option was chosen.
    Long option labels are truncated for the button face but the full
    text is what we deliver as the reply.
    """
    rows = []
    for i, opt in enumerate(options):
        face = (opt[:48] + "…") if len(opt) > 48 else opt
        rows.append([{
            "text": face,
            "callback_data": f"ask_btn:{chat_id}:{nonce}:{i}",
        }])
    return {"inline_keyboard": rows}


def _make_ask_user(bot: TelegramBot, chat_id: int, sender_id: int):
    """Returns the ask_user callback agent.run will invoke when the
    model emits an ask_user action. ``sender_id`` binds the active
    ask to the user who started the task — see _AskController."""

    def ask(*, question: str, kind: str,
            options: list[str] | None,
            attachments: list[str] | None) -> str | None:
        # Optional attachments — the model lists screenshot or file
        # paths it wants the user to see alongside the question. Send
        # them BEFORE the prompt so the question lands at the bottom
        # of the chat with the visual context already scrolled in.
        for path in attachments or []:
            try:
                if not Path(path).exists():
                    print(f"  [ask] attachment missing: {path}")
                    continue
                bot.send_photo(chat_id, path)
            except Exception as e:
                print(f"  [ask] photo {path}: {e}")

        # Resolve options. confirm = Yes/No unless overridden;
        # choose = explicit list (else degrade to text); text = no buttons.
        if kind == "confirm":
            opts = list(options) if options else ["Yes", "No"]
        elif kind == "choose":
            if not options:
                print(f"  [ask] kind=choose with no options; falling back "
                      f"to text")
                kind = "text"
                opts = []
            else:
                opts = list(options)
        else:
            kind = "text"
            opts = []

        ctrl = _AskController(kind, opts, sender_id)
        with _active_lock:
            _active_ask_controllers[chat_id] = ctrl
        try:
            try:
                if kind in ("confirm", "choose"):
                    bot.send(
                        chat_id,
                        f"❔ {question}",
                        reply_markup=_ask_button_markup(chat_id, ctrl.nonce, opts),
                    )
                else:
                    bot.send(
                        chat_id,
                        f"❔ {question}\n\n"
                        f"(Reply with text — your next message in this chat "
                        f"will be taken as the answer.)",
                    )
            except Exception as e:
                print(f"  [ask] send failed: {e} — aborting ask_user")
                return None
            decision = ctrl.wait(_ASK_USER_TIMEOUT_S)
        finally:
            with _active_lock:
                _active_ask_controllers.pop(chat_id, None)

        if decision is None:
            try:
                bot.send(chat_id,
                         "⏱ No reply in 5 min — task stopped.")
            except Exception:
                pass
            print(f"  [ask] chat={chat_id} timeout (no reply in "
                  f"{int(_ASK_USER_TIMEOUT_S)}s)")
            return None
        print(f"  [ask] chat={chat_id} → {decision[:80]!r}")
        return decision

    return ask


def _handle_ask_callback(bot: TelegramBot, cb: TelegramCallback) -> bool:
    """Returns True if this callback was an ask_btn (confirm/choose)
    button. Decodes the chosen option from callback_data and delivers
    it to the matching controller."""
    data = cb.data or ""
    if not data.startswith("ask_btn:"):
        return False
    parts = data.split(":")
    # Expected shape: ask_btn:<chat>:<nonce>:<option_index>
    if len(parts) < 4:
        try:
            bot.answer_callback(cb.callback_id, text="Malformed button")
        except Exception:
            pass
        return True
    nonce = parts[2]
    try:
        idx = int(parts[3])
    except ValueError:
        idx = -1

    chat_id = cb.chat_id
    with _active_lock:
        ctrl = _active_ask_controllers.get(chat_id)

    is_live = bool(
        ctrl is not None and nonce
        and ctrl.nonce == nonce
        and ctrl.kind in ("confirm", "choose")
        and 0 <= idx < len(ctrl.options)
        # Bind to the initiating sender — in group chats, only the user
        # who started the task should be able to answer their own ask.
        and int(cb.sender_id) == ctrl.sender_id
    )

    # Deliver before any UI cleanup (network can be slow; we don't want
    # ctrl.wait to time out before delivery).
    chosen = ""
    if is_live:
        chosen = ctrl.options[idx]   # type: ignore[union-attr]
        ctrl.deliver(chosen)         # type: ignore[union-attr]

    # Distinguish three cases for the UI cleanup:
    #   live click  → edit prompt to show the answer, strip keyboard
    #   no controller → truly stale, edit to "expired", strip keyboard
    #   wrong sender (group chat) → leave the prompt and keyboard
    #     intact so the intended user can still answer; only ack the
    #     unauthorised tap so that participant sees feedback.
    sender_mismatch = bool(
        ctrl is not None and not is_live
        and (ctrl.kind in ("confirm", "choose"))
        and int(cb.sender_id) != ctrl.sender_id
    )
    if is_live:
        try:
            bot.edit(chat_id, cb.message_id, f"❔ Answered: {chosen}",
                     reply_markup={"inline_keyboard": []})
        except Exception as e:
            print(f"  [ask] edit failed: {e}")
    elif not sender_mismatch:
        try:
            bot.edit(chat_id, cb.message_id,
                     "⌛ Question expired — click ignored.",
                     reply_markup={"inline_keyboard": []})
        except Exception as e:
            print(f"  [ask] edit failed: {e}")
    try:
        if is_live:
            bot.answer_callback(cb.callback_id, text=chosen)
        elif sender_mismatch:
            bot.answer_callback(cb.callback_id,
                                text="Only the task starter can answer",
                                show_alert=True)
        else:
            bot.answer_callback(cb.callback_id, text="Expired")
    except Exception:
        pass
    return True


def _maybe_route_message_to_ask(bot: TelegramBot,
                                msg: TelegramMessage) -> bool:
    """If a text-kind ask_user is currently waiting in this chat AND
    the sender matches the user who started the task, consume the
    message as the reply and return True (so the on_message handler
    skips the normal new-task path). Returns False otherwise.

    A reply from a different sender in a group chat falls through to
    the normal dispatcher — which will treat it as a new task or
    reject it via the worker-busy lock. The pending ask still waits
    for the original sender or its 5-min timeout.
    """
    with _active_lock:
        ctrl = _active_ask_controllers.get(msg.chat_id)
    if ctrl is None or ctrl.kind != "text":
        return False
    if int(msg.sender_id) != ctrl.sender_id:
        # Different group-chat participant — ignore their message for
        # ask routing. Do NOT consume it; let it fall through to the
        # normal task-or-reject path.
        return False
    ctrl.deliver(msg.text or "")
    try:
        bot.send(msg.chat_id, "✓ Got your reply.",
                 reply_to=msg.message_id)
    except Exception:
        pass
    return True


# ─── step check-in (every N steps the daemon asks the user to continue) ──

# Default cadence: ask after every 30 steps. Hard cap on max_steps
# stays at 200 (set in run_daemon's tg_cfg defaults).
#
# Timeout: 60 seconds. The check-in is an "intermission" — a chance
# for the user to interject if the run is going off the rails. If
# they don't reply, the task keeps going (auto-continue). Reasoning:
# the user already authorised the task by sending it, and step
# check-ins fire mid-run; defaulting to "stop" on no-reply meant a
# user away from their phone returning to find their long task
# silently aborted at step 30. ask_user (which IS a hard requirement
# for input — different controller) keeps its 5-min stop-on-timeout
# behaviour.
_STEP_CHECK_TIMEOUT_S = 60.0


class _StepCheckController:
    """Handle for the worker thread to wait on a Telegram button click.

    Each prompt gets a fresh ``nonce`` baked into its callback_data.
    The callback handler matches the received nonce against the
    controller's nonce to bind clicks to exactly the prompt that
    spawned them — robust to BOTH:
      - Stale taps on a PRIOR prompt whose buttons we failed to
        strip after edit (old nonce, no match -> ignored)
      - Taps that race ahead of bot.send returning (we set the
        nonce BEFORE sending, so the match works regardless of
        whether prompt_message_id has been recorded yet)

    ``sender_id`` is the Telegram user id who started the task. In
    group-chat deployments the per-30-step Continue / Stop buttons
    are visible to every allowed participant; without a sender bind,
    anyone in the chat can keep someone else's long-running Mac
    automation going (or stop it). Only delivery from the initiating
    sender is honored — same protection ``_AskController`` already
    applies to ask_user replies.

    nonce is set in __init__ before the controller is registered, so
    the field is never observed unset by the callback path.
    """

    def __init__(self, sender_id: int) -> None:
        self._event = threading.Event()
        self._decision: str | None = None  # "continue" | "stop" | None (timeout)
        self.sender_id = int(sender_id)
        self.nonce: str = uuid.uuid4().hex[:12]

    def deliver(self, decision: str) -> None:
        self._decision = decision
        self._event.set()

    def wait(self, timeout: float) -> str | None:
        return self._decision if self._event.wait(timeout) else None


# Keyed by chat_id. Daemon enforces 1 active task at a time, so chat_id
# uniquely identifies the in-flight controller.
_active_step_controllers: dict[int, _StepCheckController] = {}
_active_lock = threading.Lock()


def _step_callback_markup(chat_id: int, nonce: str) -> dict:
    return {
        "inline_keyboard": [[
            {"text": "Continue",
             "callback_data": f"step_continue:{chat_id}:{nonce}"},
            {"text": "Stop",
             "callback_data": f"step_stop:{chat_id}:{nonce}"},
        ]]
    }


def _make_step_check(bot: TelegramBot, chat_id: int, sender_id: int):
    """Returns the callback agent.run will invoke every N steps.
    ``sender_id`` is the user who started the task; only their button
    taps are honored (see _StepCheckController)."""

    def check(step_n: int, history: list) -> bool:
        last_name = "?"
        if history:
            try:
                last_name = history[-1].action.name
            except Exception:
                pass
        # Register the controller BEFORE we send the prompt. Otherwise
        # an instant button-tap can race the bot's poll loop: callback
        # arrives -> _handle_step_callback finds no controller -> the
        # click is discarded as "stale", and the worker would then
        # time out and stop a task the user actually chose to continue.
        ctrl = _StepCheckController(sender_id)
        with _active_lock:
            _active_step_controllers[chat_id] = ctrl
        sent_msg_id: int | None = None
        try:
            try:
                sent = bot.send(
                    chat_id,
                    f"⏸ Already {step_n} steps. Last action: {last_name}. Continue?",
                    reply_markup=_step_callback_markup(chat_id, ctrl.nonce),
                )
                # Capture the prompt message id so we can strip the
                # keyboard if the user doesn't reply in time. Without
                # this, a late Stop tap after auto-continue lands on a
                # popped controller, gets "task already ended", and
                # the still-running task keeps going — confusing UX.
                if isinstance(sent, dict):
                    sent_msg_id = int(sent.get("message_id") or 0) or None
            except Exception as e:
                print(f"  [step-check] send failed: {e} — stopping task")
                return False
            decision = ctrl.wait(_STEP_CHECK_TIMEOUT_S)
        finally:
            with _active_lock:
                _active_step_controllers.pop(chat_id, None)
        if decision == "continue":
            print(f"  [step-check] chat={chat_id} step={step_n} → continue")
            return True
        if decision is None:
            print(f"  [step-check] chat={chat_id} step={step_n} → "
                  f"timeout (no button click in {int(_STEP_CHECK_TIMEOUT_S)}s) "
                  f"— auto-continue")
            # Strip the keyboard from the original prompt so a late
            # Stop tap can't pretend to be active. Re-purpose the
            # message text to reflect the auto-continue, so the chat
            # also stays scannable.
            if sent_msg_id is not None:
                try:
                    bot.edit(
                        chat_id, sent_msg_id,
                        f"⏱ No reply in {int(_STEP_CHECK_TIMEOUT_S)}s — "
                        f"continuing automatically.",
                        reply_markup={"inline_keyboard": []},
                    )
                except Exception:
                    pass
            else:
                try:
                    bot.send(
                        chat_id,
                        f"⏱ No reply in {int(_STEP_CHECK_TIMEOUT_S)}s — "
                        f"continuing automatically.",
                    )
                except Exception:
                    pass
            return True
        print(f"  [step-check] chat={chat_id} step={step_n} → stop")
        return False

    return check


def _handle_step_callback(bot: TelegramBot, cb: TelegramCallback) -> bool:
    """Returns True if this callback was a step_continue/step_stop button.
    Dispatches the decision to the matching controller AND edits the
    original prompt message to remove the buttons + show the resolution
    so the chat doesn't accumulate dangling Continue?/Stop? prompts."""
    data = cb.data or ""
    if not (data.startswith("step_continue:") or data.startswith("step_stop:")):
        return False
    decision = "continue" if data.startswith("step_continue:") else "stop"
    # callback_data shape: "step_<decision>:<chat_id>:<nonce>". Older
    # clients (or messages from an earlier daemon version without the
    # nonce) only have two parts — treat those as stale.
    parts = data.split(":")
    recv_nonce = parts[2] if len(parts) >= 3 else ""
    chat_id = cb.chat_id
    with _active_lock:
        ctrl = _active_step_controllers.get(chat_id)

    # The click is "live" only if there is a controller, its nonce
    # matches the clicked button's callback_data, AND the tapper is
    # the user who started the task. The nonce is set in __init__
    # BEFORE the prompt is sent, so this comparison is robust to:
    #   - taps that race ahead of bot.send returning
    #   - taps on PRIOR prompts whose buttons survived a failed edit
    # The sender_id check is the group-chat authorization seal: in a
    # multi-user chat we DON'T want any allowed participant to be
    # able to authorize / abort someone else's long-running task.
    is_live = bool(
        ctrl is not None and recv_nonce and ctrl.nonce == recv_nonce
        and int(cb.sender_id) == ctrl.sender_id
    )
    sender_mismatch = bool(
        ctrl is not None and recv_nonce and ctrl.nonce == recv_nonce
        and not is_live
    )

    # Deliver the decision FIRST (in-process, instant). UI cleanup
    # follows over the network and is best-effort.
    if is_live:
        ctrl.deliver(decision)  # type: ignore[union-attr]

    # Three UI-cleanup cases:
    #   - live: edit prompt to show the decision + strip keyboard
    #   - sender mismatch: leave the prompt + keyboard intact so the
    #     intended user can still answer; only ack the wrong tapper
    #   - truly stale (no controller, or nonce mismatch): edit to
    #     "expired" and strip the keyboard
    if is_live:
        decided_text = ("✓ Continued" if decision == "continue"
                        else "⏵ Stopped")
        try:
            bot.edit(chat_id, cb.message_id, decided_text,
                     reply_markup={"inline_keyboard": []})
        except Exception as e:
            print(f"  [step-check] edit failed: {e}")
    elif not sender_mismatch:
        try:
            bot.edit(chat_id, cb.message_id,
                     "⌛ Task already ended — click ignored.",
                     reply_markup={"inline_keyboard": []})
        except Exception as e:
            print(f"  [step-check] edit failed: {e}")

    try:
        if is_live:
            bot.answer_callback(
                cb.callback_id,
                text="Continuing" if decision == "continue" else "Stopping",
            )
        elif sender_mismatch:
            bot.answer_callback(
                cb.callback_id,
                text="Only the task starter can answer",
                show_alert=True,
            )
        else:
            bot.answer_callback(cb.callback_id, text="Task already ended")
    except Exception:
        pass
    return True


def _skill_update_markup(trace_id: str) -> dict:
    return {
        "inline_keyboard": [[
            {"text": "Apply skill update", "callback_data": f"skill_apply:{trace_id}"},
            {"text": "Skip", "callback_data": f"skill_skip:{trace_id}"},
        ]]
    }


def _maybe_send_skill_update_prompt(bot: TelegramBot, chat_id: int,
                                    trace_id: str | None,
                                    reply_to: int | None) -> None:
    """Offer a Telegram button when reflection proposed a valid skill."""
    if not trace_id:
        return
    skill_body = _skill_body_from_trace(trace_id)
    if not skill_body:
        return
    parsed = parse_skill_text(skill_body)
    if parsed is None:
        print(f"  [telegram] skill proposal in trace {trace_id} has invalid frontmatter")
        return
    dry = write_user_skill(parsed.name, skill_body, dry_run=True)
    if not dry.ok:
        print(f"  [telegram] skill proposal in trace {trace_id} failed validation: {dry.error}")
        return
    text = (
        "OpenSeer found a reusable skill update.\n\n"
        f"Skill: {parsed.name}\n"
        f"Trace: {trace_id}\n\n"
        "Apply it now?"
    )
    try:
        bot.send(chat_id, text, reply_to=reply_to,
                 reply_markup=_skill_update_markup(trace_id))
    except Exception as e:
        print(f"  [telegram] skill update prompt failed: {e}")


def _handle_skill_callback(bot: TelegramBot, cb: TelegramCallback) -> None:
    data = cb.data or ""
    print(f"\n[telegram] callback {cb.sender_name} ({cb.chat_id}) → {data!r}")
    if not (data.startswith("skill_apply:") or data.startswith("skill_skip:")):
        try:
            bot.answer_callback(cb.callback_id, text="Unknown button", show_alert=True)
        except Exception:
            pass
        return

    action, trace_id = data.split(":", 1)
    if action == "skill_skip":
        try:
            bot.answer_callback(cb.callback_id, text="Skipped")
        except Exception:
            pass
        try:
            bot.edit(cb.chat_id, cb.message_id,
                     f"Skipped skill update for trace {trace_id}.",
                     reply_markup={"inline_keyboard": []})
        except Exception as e:
            print(f"  [telegram] skill skip edit failed: {e}")
        else:
            print(f"  [telegram] skipped skill update from trace {trace_id}")
        return

    skill_body = _skill_body_from_trace(trace_id)
    if not skill_body:
        msg = f"No skill proposal found for trace {trace_id}."
        try:
            bot.answer_callback(cb.callback_id, text=msg, show_alert=True)
        except Exception:
            pass
        try:
            bot.edit(cb.chat_id, cb.message_id, msg,
                     reply_markup={"inline_keyboard": []})
        except Exception as e:
            print(f"  [telegram] skill apply missing trace failed: {e}")
        return

    parsed = parse_skill_text(skill_body)
    if parsed is None:
        msg = f"Skill proposal in trace {trace_id} has invalid frontmatter."
        try:
            bot.answer_callback(cb.callback_id, text="Invalid skill proposal", show_alert=True)
        except Exception:
            pass
        try:
            bot.edit(cb.chat_id, cb.message_id, msg,
                     reply_markup={"inline_keyboard": []})
        except Exception as e:
            print(f"  [telegram] invalid skill proposal edit failed: {e}")
        return

    # Re-run the expected-name guard the in-process reflection
    # callback applies. The reflection writes ~/.openseer/runs/<id>/
    # expected_skill.txt as a sidecar; if parsed.name doesn't match
    # the file's content, the run's reflection callback would have
    # rejected this proposal in `_maybe_apply_skill`, so the
    # Telegram-side Apply must reject it too. Without this guard a
    # mis-targeted skill proposal that the in-process check already
    # rejected (e.g. lululemon facts proposed under x-com-web) could
    # still land via a button tap.
    expected_path = (Path.home() / ".openseer" / "runs"
                     / trace_id / "expected_skill.txt")
    if expected_path.exists():
        try:
            expected = expected_path.read_text(encoding="utf-8").strip()
        except Exception:
            expected = ""
        if expected and expected != parsed.name:
            msg = (f"Skill proposal name mismatch — "
                   f"proposed `{parsed.name}` but reflection expected "
                   f"`{expected}`. Edit the trace's `skill-md` block to "
                   f"use `{expected}` (or pick a different skill manually).")
            try:
                bot.answer_callback(cb.callback_id,
                                    text="Name mismatch — rejected",
                                    show_alert=True)
            except Exception:
                pass
            try:
                bot.edit(cb.chat_id, cb.message_id, msg,
                         reply_markup={"inline_keyboard": []})
            except Exception as e:
                print(f"  [telegram] skill name-mismatch edit failed: {e}")
            print(f"  [telegram] rejected skill apply: name "
                  f"{parsed.name!r} != expected {expected!r}")
            return

    res = write_user_skill(parsed.name, skill_body, dry_run=False)
    if res.ok:
        msg = f"Applied skill update: {parsed.name}\n{res.path}"
        try:
            bot.answer_callback(cb.callback_id, text="Applied")
        except Exception:
            pass
        try:
            bot.edit(cb.chat_id, cb.message_id, msg,
                     reply_markup={"inline_keyboard": []})
        except Exception as e:
            print(f"  [telegram] skill apply confirmation failed: {e}")
        print(f"  [telegram] applied skill {parsed.name} from trace {trace_id}")
        return

    msg = f"Skill update was rejected by validation:\n{res.error}"
    try:
        bot.answer_callback(cb.callback_id, text="Skill rejected", show_alert=True)
    except Exception:
        pass
    try:
        bot.edit(cb.chat_id, cb.message_id, msg,
                 reply_markup={"inline_keyboard": []})
    except Exception as e:
        print(f"  [telegram] skill rejection edit failed: {e}")


_worker_lock = threading.Lock()       # only one agent_run at a time
_worker_thread: threading.Thread | None = None

# Per-chat "generation" counter bumped by /new (and /reset). The worker
# captures the gen at task start; if it's been bumped by the time the
# task finishes, the worker skips appending its TaskSummary so a mid-run
# clear actually stays cleared. Without this, `/new` during a running
# task wipes prior history but the still-running task's summary lands
# right after, partially defeating the reset.
_session_gen: dict[int, int] = {}


_SLASH_HELP = (
    "Available commands:\n"
    "  /new       — clear this chat's session memory and start fresh\n"
    "  /reset     — alias for /new\n"
    "  /status    — show whether a task is currently running + last action\n"
    "  /help      — this list\n\n"
    "Anything else is treated as a task. The daemon runs one task at a "
    "time on this Mac; if a task is already running you'll get a "
    "\"task in progress\" reply (slash commands always work)."
)


def _maybe_handle_slash(bot: TelegramBot, sessions: ChatSessions,
                       msg: TelegramMessage,
                       our_username: str | None = None) -> bool:
    """Lightweight chat-control commands. Returns True if the message
    was handled as a command (caller should NOT treat it as a task).

    Commands are case-insensitive, must be the entire message body
    (no inline arguments yet), and tolerate the @bot suffix Telegram
    auto-appends in groups (`/new@seer_1101_bot`).

    ``our_username`` is the daemon's own bot username (without `@`).
    When provided, an explicit `@OtherBot` suffix is rejected so a
    command addressed to a different bot in the same group can't
    accidentally drive this daemon (e.g. `/new@OtherBot` should NOT
    clear OpenSeer's session memory)."""
    cmd = (msg.text or "").strip()
    if not cmd.startswith("/"):
        return False
    parts = cmd.split()
    head_full = parts[0]
    has_args = len(parts) > 1
    # Honor explicit @suffix: only accept when missing or matching us.
    if "@" in head_full:
        verb, _, target = head_full.partition("@")
        if our_username and target.lower() != our_username.lower():
            # Command for a different bot — silently ignore so the
            # other bot can handle it. Don't reply with help.
            return True
        head = verb.lower()
    else:
        head = head_full.lower()

    # None of the current commands accept inline arguments. Reject
    # `/new buy tickets` etc. with help — the destructive ones (/new,
    # /reset) MUST NOT silently fire when the user supplied extra text
    # they probably intended as task input. Codex flagged this case.
    if has_args:
        try:
            bot.send(
                msg.chat_id,
                (f"Command {head!r} doesn't take arguments. "
                 f"Did you mean to send a task without the slash, or "
                 f"to run the bare command? " + _SLASH_HELP),
                reply_to=msg.message_id,
            )
        except Exception:
            pass
        return True

    if head in ("/new", "/reset"):
        # Clear and bump the gen as ONE atomic operation under the
        # _active_lock — same lock the worker uses around its
        # gen-check + sessions.append. Without this, the worker can
        # observe gen=match, release the lock, and then this handler
        # squeezes in a clear+bump before the worker's append, which
        # repopulates the just-reset chat. (Codex flagged that race.)
        with _active_lock:
            n = sessions.clear(msg.chat_id)
            _session_gen[msg.chat_id] = _session_gen.get(msg.chat_id, 0) + 1
        try:
            bot.send(
                msg.chat_id,
                (f"🧹 Session memory cleared ({n} prior task"
                 f"{'s' if n != 1 else ''} forgotten). "
                 f"Send your next task fresh."),
                reply_to=msg.message_id,
            )
        except Exception:
            pass
        return True

    if head == "/status":
        running = bool(_worker_thread and _worker_thread.is_alive())
        prior = sessions.history(msg.chat_id)
        last = prior[-1] if prior else None
        lines = [
            f"running: {'yes' if running else 'no'}",
            f"session memory: {len(prior)} prior task"
            f"{'s' if len(prior) != 1 else ''}",
        ]
        if last:
            lines.append(f"last task: {last.task[:80]!r} → {last.status}")
        try:
            bot.send(msg.chat_id, "\n".join(lines), reply_to=msg.message_id)
        except Exception:
            pass
        return True

    if head == "/help":
        try:
            bot.send(msg.chat_id, _SLASH_HELP, reply_to=msg.message_id)
        except Exception:
            pass
        return True

    # Unknown slash command — answer with help so the user discovers
    # the real ones. Without this branch a typo'd `/news` would fall
    # through and become a task ("/news" sent to the model).
    try:
        bot.send(
            msg.chat_id,
            f"Unknown command {head!r}. {_SLASH_HELP}",
            reply_to=msg.message_id,
        )
    except Exception:
        pass
    return True


def _make_dispatcher(bot: TelegramBot, sessions: ChatSessions, *,
                     max_steps: int, step_check_interval: int,
                     confirm_each: bool,
                     bot_username: str | None = None):
    """Returns the on_message callback. Captures bot + session store.

    Each incoming message spawns a background worker thread that runs
    `agent_run` plus post-task work. The bot's main poll loop stays on
    the main thread so it can keep ingesting `callback_query` updates
    (used by the per-30-step "Continue?" buttons and skill-update
    buttons). Only one worker runs at a time — Mac mouse/keyboard
    can't be shared.
    """

    def _worker(msg: TelegramMessage, ack_msg_id: int,
                start_gen: int) -> None:
        # `start_gen` is captured in on_message synchronously BEFORE
        # the worker thread is spawned. Reading it here would race
        # with a /new sent in the gap between thread.start() and the
        # worker actually scheduling — codex flagged that case.
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

        step_check = (_make_step_check(bot, msg.chat_id, msg.sender_id)
                      if step_check_interval > 0 else None)
        ask_user_cb = _make_ask_user(bot, msg.chat_id, msg.sender_id)

        t0 = time.time()
        try:
            history = agent_run(
                msg.text,
                max_steps=max_steps,
                dry_run=False,
                confirm_each=confirm_each,
                callbacks=callbacks,
                session_context=session_context,
                step_check_interval=step_check_interval,
                step_check=step_check,
                ask_user=ask_user_cb,
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
            return
        except Exception as e:
            print(f"  [agent] error: {e!r}")
            try:
                bot.send(msg.chat_id, f"✗ run errored: {e}", reply_to=msg.message_id)
            except Exception:
                pass
            return

        dur = time.time() - t0
        result = _format_result(history, dur)
        trace_id = _latest_trace_id()
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

        _maybe_send_skill_update_prompt(
            bot, msg.chat_id, trace_id, ack_msg_id or msg.message_id,
        )

        status, result_text = _canonical_status(history)
        # Skip the session-memory append if the user ran /new (or
        # /reset) while this task was in flight. Combine the gen
        # check AND the append into a single critical section under
        # _active_lock so the slash handler can't squeeze a
        # clear+bump in between (codex P2 race).
        appended = False
        with _active_lock:
            current_gen = _session_gen.get(msg.chat_id, 0)
            if current_gen == start_gen:
                sessions.append(msg.chat_id, TaskSummary(
                    task=msg.text,
                    status=status,
                    result=(result_text or "")[:300],
                    trace_id=trace_id,
                    ts=time.time(),
                ))
                appended = True
        if not appended:
            print(f"[telegram] session was /new'd mid-task; "
                  f"skipping post-task append for chat {msg.chat_id}")
            print(f"[telegram] replied ({dur:.1f}s, {len(history)} steps, "
                  f"status={status}, post-reset)")
            return
        print(f"[telegram] replied ({dur:.1f}s, {len(history)} steps, status={status})")

    def on_message(msg: TelegramMessage) -> None:
        global _worker_thread
        print(f"\n[telegram] {msg.sender_name} ({msg.chat_id}) → {msg.text[:80]!r}")

        # Slash commands FIRST — even before ask_user routing. A user
        # who's mid-conversation with a text ask_user might still want
        # to /new or /status to bail out; consuming `/new` as the
        # ask reply would be confusing. The handler returns False for
        # non-slash messages so normal flow continues.
        if _maybe_handle_slash(bot, sessions, msg, bot_username):
            return

        # If a text-kind ask_user is currently waiting in this chat,
        # the user's message is the answer to the question — NOT a new
        # task. Route it to the controller and return; the worker
        # thread that called ask_user is blocked on its event and will
        # resume now.
        if _maybe_route_message_to_ask(bot, msg):
            return

        # Reject if a task is already running. Mac CU can't multiplex
        # mouse/keyboard, so concurrent agent_runs would clobber each
        # other. Telling the user is more useful than silently queueing.
        if _worker_thread is not None and _worker_thread.is_alive():
            try:
                bot.send(
                    msg.chat_id,
                    "⏳ Another task is still running. Wait for it to "
                    "finish, or tap Stop on its check-in to free the daemon.",
                    reply_to=msg.message_id,
                )
            except Exception:
                pass
            return

        try:
            ack = bot.send(msg.chat_id,
                           f"⏳ working on:\n{msg.text[:200]}",
                           reply_to=msg.message_id)
            ack_msg_id = int(ack.get("message_id", 0))
        except Exception as e:
            print(f"  [telegram] ack failed: {e}")
            ack_msg_id = 0

        # Capture the chat's session generation BEFORE spawning the
        # worker. If we read it inside the worker thread, a /new sent
        # in the gap between thread.start() and the worker actually
        # being scheduled would land its bump first, making
        # start_gen == current_gen at the end so the worker would
        # append its summary into the just-cleared session.
        with _active_lock:
            start_gen = _session_gen.get(msg.chat_id, 0)

        # Spawn worker. daemon=True so the thread doesn't block process
        # exit on Ctrl+C (the worker's blocking subprocess calls are
        # generally non-cancellable from outside; we accept that the
        # current step may finish unfinished if the user hard-quits).
        def _run() -> None:
            try:
                _worker(msg, ack_msg_id, start_gen)
            finally:
                _worker_lock.release()

        if not _worker_lock.acquire(blocking=False):
            # Race: another thread grabbed the lock between the check
            # above and now. Tell the user.
            try:
                bot.send(msg.chat_id,
                         "⏳ Another task started just before yours. Wait.",
                         reply_to=msg.message_id)
            except Exception:
                pass
            return
        t = threading.Thread(target=_run, daemon=True, name="openseer-worker")
        _worker_thread = t
        t.start()

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

    # Defaults applied with `is None` (not `or`) so an explicit 0 in
    # the config disables the feature instead of being replaced by
    # the fallback. Same pattern for max_steps in case someone sets
    # a smaller cap on purpose.
    _max_steps_cfg = tg_cfg.get("max_steps")
    _interval_cfg = tg_cfg.get("step_check_interval")
    on_msg = _make_dispatcher(
        bot, sessions,
        max_steps=int(_max_steps_cfg) if _max_steps_cfg is not None else 200,
        step_check_interval=int(_interval_cfg) if _interval_cfg is not None else 30,
        confirm_each=bool(tg_cfg.get("confirm_each", False)),
        bot_username=str(me.get("username") or "") or None,
    )

    # Callback router: step_* check-in buttons → step controller,
    # ask_btn:* (confirm/choose) → ask controller,
    # everything else falls through to the existing skill-update handler.
    def _on_callback(cb: TelegramCallback) -> None:
        if _handle_step_callback(bot, cb):
            return
        if _handle_ask_callback(bot, cb):
            return
        _handle_skill_callback(bot, cb)

    # Bypass the trigger_prefix filter for two cases:
    #   1. Active text-kind ask_user from this exact sender — they're
    #      answering a question, not starting a task.
    #   2. Slash commands (/new, /status, /help, etc.) — these are
    #      chat-control commands that should always work even when
    #      `trigger_prefix: "openseer:"` is configured. Without this,
    #      `/new` would be silently dropped because it doesn't start
    #      with the prefix, and the user couldn't reset session
    #      memory at all in prefixed setups.
    # Sender-narrowed for case 1 to avoid leaking unrelated group-chat
    # traffic past the prefix filter.
    def _bypass_prefix(chat_id: int, sender_id: int,
                       text: str | None = None) -> bool:
        if text and text.lstrip().startswith("/"):
            return True
        with _active_lock:
            ctrl = _active_ask_controllers.get(chat_id)
        return (
            ctrl is not None
            and ctrl.kind == "text"
            and int(sender_id) == ctrl.sender_id
        )

    try:
        bot.poll(on_msg, on_callback=_on_callback,
                 bypass_prefix=_bypass_prefix)
    except KeyboardInterrupt:
        pass
    print("daemon: stopped.")
    return 0
