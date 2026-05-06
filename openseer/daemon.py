"""Long-running daemon mode: `openseer daemon`.

Listens on configured inbound channels (Telegram for now), turns each
message into a task, runs it through the agent loop, replies with the
result. One task at a time — we don't run concurrent tasks because they
would fight for keyboard/mouse focus on the user's Mac.

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

allowed_chat_ids is the safety door: only those chats can issue tasks.
trigger_prefix lets you keep chatting normally with the bot without
every message becoming a task.
"""
from __future__ import annotations

import json
import signal
import sys
import time
from pathlib import Path

from .agent import OAI_MODEL, run as agent_run
from .callbacks import (
    BudgetCallback, ImageRetentionCallback, SafetyCallback,
    TrajectoryCallback,
)
from .inbox.telegram import TelegramBot, TelegramMessage


_CONFIG_PATH = Path.home() / ".openseer" / "config.json"


def _load_config() -> dict:
    if not _CONFIG_PATH.exists():
        return {}
    try:
        return json.loads(_CONFIG_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _format_result(task: str, history: list, dur_s: float) -> str:
    """Compact end-of-task summary fitting one Telegram message."""
    last = history[-1] if history else None
    if not last:
        return f"⚠ no steps executed ({dur_s:.1f}s)"
    a = last.action
    if a.name == "terminate":
        st = (a.status or "done").lower()
        glyph = "✓" if st == "done" else "⚠"
        reason = a.reason or ""
        head = f"{glyph} {st}  {len(history)} steps · {dur_s:.1f}s"
        if reason:
            return f"{head}\n\n{reason}"
        return head
    if a.name in ("done", "fail"):
        glyph = "✓" if a.name == "done" else "⚠"
        return f"{glyph} {a.name}  {len(history)} steps · {dur_s:.1f}s\n\n{a.reason or ''}"
    # Hit step cap or aborted by safety
    return f"• stopped at step {len(history)} ({dur_s:.1f}s) — last: {a.name}"


def _make_dispatcher(bot: TelegramBot, *, max_steps: int, confirm_each: bool):
    """Returns the on_message callback. Captures `bot` so we can reply."""
    def on_message(msg: TelegramMessage) -> None:
        print(f"\n[telegram] {msg.sender_name} → {msg.text[:80]!r}")
        # Acknowledge receipt so the user knows we're working
        try:
            bot.send(msg.chat_id,
                     f"⏳ working on:\n{msg.text[:200]}",
                     reply_to=msg.message_id)
        except Exception as e:
            print(f"  [telegram] ack send failed: {e}")

        t0 = time.time()
        try:
            history = agent_run(
                msg.text,
                max_steps=max_steps,
                dry_run=False,
                confirm_each=confirm_each,
                quiet=False,
            )
        except KeyboardInterrupt:
            try:
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
        result = _format_result(msg.text, history, dur)
        try:
            bot.send(msg.chat_id, result, reply_to=msg.message_id)
        except Exception as e:
            print(f"  [telegram] reply send failed: {e}")
        print(f"[telegram] replied ({dur:.1f}s, {len(history)} steps)")

    return on_message


def run_daemon() -> int:
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

    # Probe token early so the user gets immediate feedback if it's bad.
    try:
        me = bot.get_me()
    except Exception as e:
        print(f"daemon: telegram getMe failed — {e}")
        print("       check the token in ~/.openseer/config.json.")
        return 1
    print(f"daemon: telegram bot @{me.get('username')} ({me.get('first_name')}) ready")
    print(f"        provider={OAI_MODEL}")
    if bot.allowed_chat_ids:
        print(f"        allowed chat_ids: {sorted(bot.allowed_chat_ids)}")
    else:
        print(f"        ⚠ no allowed_chat_ids — anyone who finds the bot can issue tasks")
    if bot.trigger_prefix:
        print(f"        trigger prefix: {bot.trigger_prefix!r}")
    print("        (Ctrl+C to stop)")

    # Allow Ctrl+C to break out of the long-poll cleanly.
    def _on_sigint(signum, frame):
        print("\ndaemon: stop signal — finishing current poll cycle …")
        bot.stop()
    signal.signal(signal.SIGINT, _on_sigint)
    signal.signal(signal.SIGTERM, _on_sigint)

    on_msg = _make_dispatcher(
        bot,
        max_steps=int(tg_cfg.get("max_steps") or 25),
        confirm_each=bool(tg_cfg.get("confirm_each", False)),
    )
    try:
        bot.poll(on_msg)
    except KeyboardInterrupt:
        pass
    print("daemon: stopped.")
    return 0
