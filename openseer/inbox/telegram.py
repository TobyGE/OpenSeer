"""Telegram Bot inbound channel — phone → OpenSeer task dispatch.

Architecture (kept tiny on purpose):

  Telegram Bot API <─── HTTPS long-poll ───  TelegramBot.poll()
                                                  │
                                            on_message(msg) callback
                                                  │
                                       allowed_chat_ids check
                                                  │
                                       trigger_prefix check
                                                  │
                                       dispatch as task → agent.run()
                                                  │
                                       bot.send(chat_id, formatted result)

Why no python-telegram-bot dep:
  The Bot API is ~3 endpoints we use. urllib + json is 100 lines vs
  pulling in an async framework. Same reason we don't use openai/anthropic
  SDKs — keep the surface small.

Auth:
  Token comes from BotFather (@BotFather → /newbot → copy token).
  Allowed chat IDs are the integer IDs of chats that are allowed to
  send tasks. Get yours by messaging the bot once and checking the
  log; only those chat_ids can issue tasks.
"""
from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Callable


_API = "https://api.telegram.org"


def _chunk_text(text: str, chunk_size: int) -> list[str]:
    """Split `text` into ≤chunk_size pieces, preferring paragraph then
    word boundaries. Preserves ALL whitespace — code blocks, indented
    text, and trailing newlines around chunk boundaries are kept
    intact. The only addition is the chunk-counter header that callers
    prepend later (which is structural)."""
    if not text:
        return []
    if len(text) <= chunk_size:
        return [text]
    out: list[str] = []
    pos = 0
    n = len(text)
    while n - pos > chunk_size:
        # Pick the latest boundary inside [pos, pos+chunk_size). Cut AFTER
        # the boundary character so newlines / spaces stay with the
        # previous chunk (they're whitespace the user wrote, not noise).
        window_end = pos + chunk_size
        # paragraph boundary: keep the second \n at end of prev chunk
        cut = text.rfind("\n\n", pos, window_end)
        if cut >= pos + chunk_size // 2:
            cut += 2                      # split AFTER the \n\n
        else:
            cut = text.rfind("\n", pos, window_end)
            if cut >= pos + chunk_size // 2:
                cut += 1                  # AFTER the \n
            else:
                cut = text.rfind(" ", pos, window_end)
                if cut >= pos + chunk_size // 2:
                    cut += 1              # AFTER the space
                else:
                    cut = window_end      # hard cut, no good boundary
        out.append(text[pos:cut])
        pos = cut
    if pos < n:
        out.append(text[pos:])
    return out


@dataclass
class TelegramMessage:
    """A subset of a Telegram Update→Message we actually care about."""
    update_id: int
    message_id: int
    chat_id: int
    chat_title: str          # group name OR username/first_name for DMs
    sender_id: int
    sender_name: str
    text: str
    date: int                # unix seconds


class TelegramError(RuntimeError):
    pass


def _http_post(url: str, params: dict, *, timeout: float = 35.0) -> dict:
    body = urllib.parse.urlencode(params).encode("utf-8")
    req = urllib.request.Request(
        url, data=body, method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body_text = ""
        try:
            body_text = e.read().decode("utf-8")
        except Exception:
            pass
        raise TelegramError(f"HTTP {e.code}: {body_text[:300]}") from e
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise TelegramError(f"network: {e}") from e
    if not data.get("ok"):
        raise TelegramError(
            f"API error code={data.get('error_code')} desc={data.get('description')!r}"
        )
    return data["result"]


class TelegramBot:
    """Polling bot client. Single-threaded by design — one poll loop,
    one send at a time. The agent loop blocks the daemon for the full
    duration of a task; that's intentional (we don't want concurrent
    tasks fighting for keyboard/mouse focus on the user's Mac)."""

    def __init__(self, token: str, *, allowed_chat_ids: list[int] | None = None,
                 trigger_prefix: str | None = None,
                 poll_timeout: int = 30) -> None:
        self.token = token.strip()
        if not self.token:
            raise ValueError("telegram token is empty")
        self.allowed_chat_ids = set(allowed_chat_ids or [])
        self.trigger_prefix = (trigger_prefix or "").strip()
        self.poll_timeout = poll_timeout
        self._offset = 0
        self._stop = threading.Event()

    # ───── HTTP wrappers ────────────────────────────────────────────────
    def _call(self, method: str, params: dict, *, timeout: float | None = None) -> dict:
        url = f"{_API}/bot{self.token}/{method}"
        return _http_post(url, params,
                          timeout=timeout if timeout is not None else 35.0)

    def get_me(self) -> dict:
        """Verify token + return bot identity. Raises on bad token."""
        return self._call("getMe", {}, timeout=10.0)

    def send(self, chat_id: int, text: str, *,
             reply_to: int | None = None,
             parse_mode: str | None = None) -> dict:
        """Single message (caller is responsible for length). Truncates
        to 4090 if over. Use ``send_long`` for multi-chunk delivery."""
        params = {"chat_id": chat_id, "text": text[:4090]}
        if reply_to is not None:
            params["reply_to_message_id"] = reply_to
        if parse_mode:
            params["parse_mode"] = parse_mode
        return self._call("sendMessage", params, timeout=15.0)

    def send_long(self, chat_id: int, text: str, *,
                  reply_to: int | None = None,
                  parse_mode: str | None = None,
                  chunk_size: int = 3800) -> list[dict]:
        """Send `text` across one or more messages, chunking on
        paragraph then word boundaries so the user's content isn't
        silently sliced. Each chunk replies to the previous one (so
        Telegram threads them visually). Returns the list of API
        responses, one per chunk."""
        if not text:
            return []
        chunks = _chunk_text(text, chunk_size)
        out: list[dict] = []
        prev_reply = reply_to
        for i, chunk in enumerate(chunks):
            header = f"({i + 1}/{len(chunks)})\n" if len(chunks) > 1 else ""
            r = self.send(chat_id, header + chunk,
                          reply_to=prev_reply, parse_mode=parse_mode)
            out.append(r)
            try:
                prev_reply = int(r.get("message_id", 0)) or prev_reply
            except Exception:
                pass
        return out

    def edit(self, chat_id: int, message_id: int, text: str, *,
             parse_mode: str | None = None) -> dict | None:
        """Edit a previously-sent message in place. Returns None if
        Telegram rejects the edit (commonly: identical content; the API
        raises 'message is not modified' which we silently ignore so
        the caller doesn't have to think about throttling)."""
        params = {
            "chat_id": chat_id,
            "message_id": message_id,
            "text": text[:4090],
        }
        if parse_mode:
            params["parse_mode"] = parse_mode
        try:
            return self._call("editMessageText", params, timeout=15.0)
        except TelegramError as e:
            es = str(e)
            if "not modified" in es or "message is not modified" in es:
                return None
            raise

    # ───── polling ──────────────────────────────────────────────────────
    def stop(self) -> None:
        self._stop.set()

    def poll(self, on_message: Callable[[TelegramMessage], None]) -> None:
        """Long-poll forever. Blocks until stop() is called or process dies.

        Calls `on_message(msg)` for each NEW message that:
          - has text (we ignore stickers, voice, photos for now)
          - comes from an allowed chat_id (if allowlist set)
          - starts with trigger_prefix (if set)

        on_message exceptions are caught and logged so one bad task
        doesn't kill the whole daemon.
        """
        while not self._stop.is_set():
            try:
                updates = self._call("getUpdates", {
                    "offset": self._offset,
                    "timeout": self.poll_timeout,
                    "allowed_updates": json.dumps(["message"]),
                }, timeout=self.poll_timeout + 10.0)
            except TelegramError as e:
                # Transient — back off and retry
                print(f"  [telegram] poll error: {e}; sleeping 5s")
                self._stop.wait(5.0)
                continue

            for upd in updates:
                self._offset = max(self._offset, int(upd["update_id"]) + 1)
                msg_obj = upd.get("message")
                if not msg_obj:
                    continue
                text = (msg_obj.get("text") or "").strip()
                if not text:
                    continue                    # skip non-text messages

                chat = msg_obj.get("chat") or {}
                sender = msg_obj.get("from") or {}
                chat_id = int(chat.get("id", 0))
                if self.allowed_chat_ids and chat_id not in self.allowed_chat_ids:
                    print(f"  [telegram] dropped msg from non-allowed "
                          f"chat_id={chat_id} ({sender.get('first_name', '?')})")
                    continue

                if self.trigger_prefix:
                    if not text.startswith(self.trigger_prefix):
                        continue
                    text = text[len(self.trigger_prefix):].strip()
                    if not text:
                        continue

                msg = TelegramMessage(
                    update_id=int(upd["update_id"]),
                    message_id=int(msg_obj.get("message_id", 0)),
                    chat_id=chat_id,
                    chat_title=(chat.get("title") or chat.get("username")
                                or chat.get("first_name") or str(chat_id)),
                    sender_id=int(sender.get("id", 0)),
                    sender_name=(sender.get("first_name") or sender.get("username")
                                 or str(sender.get("id", 0))),
                    text=text,
                    date=int(msg_obj.get("date", 0)),
                )
                try:
                    on_message(msg)
                except Exception as e:
                    print(f"  [telegram] on_message raised: {e!r}")
