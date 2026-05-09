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


@dataclass
class TelegramCallback:
    """A Telegram inline-button callback_query."""
    update_id: int
    callback_id: str
    chat_id: int
    message_id: int
    sender_id: int
    sender_name: str
    data: str


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
                 poll_timeout: int = 30,
                 offset_state_path: "Path | None" = None) -> None:
        self.token = token.strip()
        if not self.token:
            raise ValueError("telegram token is empty")
        self.allowed_chat_ids = set(allowed_chat_ids or [])
        self.trigger_prefix = (trigger_prefix or "").strip()
        self.poll_timeout = poll_timeout
        self._stop = threading.Event()
        # Persist the update offset so a daemon restart doesn't re-deliver
        # already-handled commands. Telegram retains undelivered updates
        # for 24h; without persistence, a crash mid-task can cause the
        # SAME Mac command to execute twice on the next start.
        # Key the file by bot id (integer prefix before `:`) so swapping
        # tokens doesn't shadow a fresh bot's updates with the old bot's
        # offset.
        from pathlib import Path
        bot_id = self.token.split(":", 1)[0] or "unknown"
        # `bot_id` is digits only by Telegram's spec, so it's safe in a
        # filename; we still strip path separators defensively.
        bot_id = "".join(c for c in bot_id if c.isalnum()) or "unknown"
        self._offset_state_path = (
            offset_state_path
            or (Path.home() / ".openseer" / "inbox"
                / f"telegram_offset_{bot_id}.json")
        )
        self._offset = self._load_offset()

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
             parse_mode: str | None = None,
             reply_markup: dict | None = None) -> dict:
        """Single message (caller is responsible for length). Truncates
        to 4090 if over. Use ``send_long`` for multi-chunk delivery."""
        params = {"chat_id": chat_id, "text": text[:4090]}
        if reply_to is not None:
            params["reply_to_message_id"] = reply_to
        if parse_mode:
            params["parse_mode"] = parse_mode
        if reply_markup:
            params["reply_markup"] = json.dumps(reply_markup)
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

    def send_photo(self, chat_id: int, image_path: str, *,
                   caption: str | None = None,
                   reply_to: int | None = None) -> dict:
        """Upload an image file via multipart/form-data sendPhoto.
        Telegram caps photo files at 10 MB; we don't auto-resize, just
        let the API reject oversized."""
        import os
        url = f"{_API}/bot{self.token}/sendPhoto"
        boundary = "----openseer-" + os.urandom(8).hex()
        ct = "image/png"
        if image_path.lower().endswith((".jpg", ".jpeg")):
            ct = "image/jpeg"
        elif image_path.lower().endswith(".gif"):
            ct = "image/gif"
        with open(image_path, "rb") as f:
            file_bytes = f.read()
        body = bytearray()

        def _field(name: str, value: str) -> None:
            body.extend(f"--{boundary}\r\n".encode())
            body.extend(f'Content-Disposition: form-data; name="{name}"\r\n\r\n'
                        .encode())
            body.extend(value.encode("utf-8"))
            body.extend(b"\r\n")

        _field("chat_id", str(chat_id))
        if caption:
            _field("caption", caption[:1024])      # Telegram caption limit
        if reply_to is not None:
            _field("reply_to_message_id", str(reply_to))
        # photo file
        body.extend(f"--{boundary}\r\n".encode())
        body.extend(
            f'Content-Disposition: form-data; name="photo"; '
            f'filename="{os.path.basename(image_path)}"\r\n'.encode()
        )
        body.extend(f"Content-Type: {ct}\r\n\r\n".encode())
        body.extend(file_bytes)
        body.extend(f"\r\n--{boundary}--\r\n".encode())

        req = urllib.request.Request(
            url, data=bytes(body), method="POST",
            headers={
                "Content-Type": f"multipart/form-data; boundary={boundary}",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=30.0) as r:
                data = json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            body_text = ""
            try:
                body_text = e.read().decode("utf-8")
            except Exception:
                pass
            raise TelegramError(f"sendPhoto HTTP {e.code}: {body_text[:300]}") from e
        if not data.get("ok"):
            raise TelegramError(f"sendPhoto: {data}")
        return data["result"]

    def edit(self, chat_id: int, message_id: int, text: str, *,
             parse_mode: str | None = None,
             reply_markup: dict | None = None) -> dict | None:
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
        if reply_markup:
            params["reply_markup"] = json.dumps(reply_markup)
        try:
            return self._call("editMessageText", params, timeout=15.0)
        except TelegramError as e:
            es = str(e)
            if "not modified" in es or "message is not modified" in es:
                return None
            raise

    def answer_callback(self, callback_query_id: str, *,
                        text: str | None = None,
                        show_alert: bool = False) -> dict:
        params = {
            "callback_query_id": callback_query_id,
            "show_alert": json.dumps(bool(show_alert)),
        }
        if text:
            params["text"] = text[:200]
        return self._call("answerCallbackQuery", params, timeout=10.0)

    # ───── offset persistence ────────────────────────────────────────────
    def _load_offset(self) -> int:
        try:
            return int(json.loads(self._offset_state_path.read_text())["offset"])
        except Exception:
            return 0

    def _save_offset(self, offset: int) -> None:
        try:
            self._offset_state_path.parent.mkdir(parents=True, exist_ok=True)
            self._offset_state_path.write_text(
                json.dumps({"offset": int(offset)}), encoding="utf-8"
            )
        except Exception as e:
            print(f"  [telegram] couldn't persist offset: {e}")

    # ───── polling ──────────────────────────────────────────────────────
    def stop(self) -> None:
        self._stop.set()

    def poll(self, on_message: Callable[[TelegramMessage], None],
             on_callback: Callable[[TelegramCallback], None] | None = None,
             bypass_prefix: Callable[..., bool] | None = None) -> None:
        """Long-poll forever. Blocks until stop() is called or process dies.

        Calls `on_message(msg)` for each NEW message that:
          - has text (we ignore stickers, voice, photos for now)
          - comes from an allowed chat_id (if allowlist set)
          - starts with trigger_prefix (if set), UNLESS ``bypass_prefix``
            is provided and returns True for ``(chat_id, sender_id)``.
            The bypass hook is what lets the daemon route a free-form
            text reply to an active ``ask_user(kind="text")`` even when
            the user's normal task-trigger prefix is set — replies
            aren't tasks. The hook receives sender_id so a group-chat
            bypass only relaxes filtering for the user who actually
            started the task; everyone else's plain-text traffic
            still gets dropped by the prefix filter.
        Also dispatches inline-button callback_query updates to
        ``on_callback`` when provided.

        on_message exceptions are caught and logged so one bad task
        doesn't kill the whole daemon.
        """
        while not self._stop.is_set():
            try:
                updates = self._call("getUpdates", {
                    "offset": self._offset,
                    "timeout": self.poll_timeout,
                    "allowed_updates": json.dumps(["message", "callback_query"]),
                }, timeout=self.poll_timeout + 10.0)
            except TelegramError as e:
                # Transient — back off and retry
                print(f"  [telegram] poll error: {e}; sleeping 5s")
                self._stop.wait(5.0)
                continue

            for upd in updates:
                # Persist the offset BEFORE dispatch so a crash during
                # task execution can't replay the command on restart.
                # Telegram considers an update acknowledged once we ask
                # for an offset > update_id.
                new_offset = int(upd["update_id"]) + 1
                if new_offset > self._offset:
                    self._offset = new_offset
                    self._save_offset(self._offset)
                cb_obj = upd.get("callback_query")
                if cb_obj:
                    if on_callback is None:
                        continue
                    msg_for_cb = cb_obj.get("message") or {}
                    chat = msg_for_cb.get("chat") or {}
                    sender = cb_obj.get("from") or {}
                    chat_id = int(chat.get("id", 0))
                    if not self.allowed_chat_ids:
                        print(f"  [telegram] no allowed_chat_ids configured — "
                              f"dropped callback from chat_id={chat_id} "
                              f"({sender.get('first_name', '?')}).")
                        continue
                    if chat_id not in self.allowed_chat_ids:
                        print(f"  [telegram] dropped callback from non-allowed "
                              f"chat_id={chat_id} ({sender.get('first_name', '?')})")
                        continue
                    cb = TelegramCallback(
                        update_id=int(upd["update_id"]),
                        callback_id=str(cb_obj.get("id", "")),
                        chat_id=chat_id,
                        message_id=int(msg_for_cb.get("message_id", 0)),
                        sender_id=int(sender.get("id", 0)),
                        sender_name=(sender.get("first_name") or sender.get("username")
                                     or str(sender.get("id", 0))),
                        data=str(cb_obj.get("data") or ""),
                    )
                    try:
                        on_callback(cb)
                    except Exception as e:
                        print(f"  [telegram] on_callback raised: {e!r}")
                    continue

                msg_obj = upd.get("message")
                if not msg_obj:
                    continue
                text = (msg_obj.get("text") or "").strip()
                if not text:
                    continue                    # skip non-text messages

                chat = msg_obj.get("chat") or {}
                sender = msg_obj.get("from") or {}
                chat_id = int(chat.get("id", 0))
                # Fail CLOSED on missing allowlist: an empty set must NOT
                # mean "allow everyone". The daemon executes real Mac
                # actions (dry_run=False), so any chat that can find the
                # bot would be able to drive the user's computer. We log
                # the chat_id so the user can copy it into config, but
                # do not dispatch the message as a task.
                if not self.allowed_chat_ids:
                    print(f"  [telegram] no allowed_chat_ids configured — "
                          f"dropped message from chat_id={chat_id} "
                          f"({sender.get('first_name', '?')}). "
                          f"Add this id to config to enable.")
                    continue
                if chat_id not in self.allowed_chat_ids:
                    print(f"  [telegram] dropped msg from non-allowed "
                          f"chat_id={chat_id} ({sender.get('first_name', '?')})")
                    continue

                if self.trigger_prefix:
                    sender_id = int(sender.get("id", 0))
                    # Pass the raw text too so the bypass hook can let
                    # slash commands (/new, /status, etc.) through even
                    # when they don't have the configured prefix.
                    if bypass_prefix:
                        try:
                            skip_prefix = bool(
                                bypass_prefix(chat_id, sender_id, text)
                            )
                        except TypeError:
                            # Backwards-compat with older 2-arg hook.
                            skip_prefix = bool(
                                bypass_prefix(chat_id, sender_id)
                            )
                    else:
                        skip_prefix = False
                    if not skip_prefix:
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
