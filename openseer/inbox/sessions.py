"""Per-chat session memory for inbound channels (Telegram, …).

Each chat_id gets a bounded ring of recent task summaries so a follow-up
message has the prior task as context (mirrors the REPL session memory,
just keyed by chat_id and persisted to disk).

Stored at ``~/.openseer/inbox/sessions.json``::

    {
      "<chat_id>": [
        {"task":"...","status":"done","result":"...","trace_id":"...","ts":...},
        ...
      ]
    }

Bounded to MAX_PER_CHAT entries per chat to avoid unbounded growth.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, asdict
from pathlib import Path
from threading import Lock


_PATH = Path.home() / ".openseer" / "inbox" / "sessions.json"
MAX_PER_CHAT = 5


@dataclass
class TaskSummary:
    task: str
    status: str            # done | fail | verify_failed | cap | error
    result: str            # last action's reason, truncated for prompt
    trace_id: str | None
    ts: float              # unix seconds


class ChatSessions:
    """Thread-safe load/append/persist for per-chat task histories."""

    def __init__(self, path: Path = _PATH) -> None:
        self.path = path
        self._lock = Lock()
        self._data: dict[str, list[TaskSummary]] = {}
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception:
            return
        for chat_id, rows in (raw or {}).items():
            try:
                self._data[str(chat_id)] = [
                    TaskSummary(**row) for row in rows[-MAX_PER_CHAT:]
                ]
            except Exception:
                pass

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        out = {
            cid: [asdict(s) for s in rows]
            for cid, rows in self._data.items()
        }
        self.path.write_text(json.dumps(out, ensure_ascii=False, indent=2),
                             encoding="utf-8")

    def history(self, chat_id: int | str) -> list[TaskSummary]:
        with self._lock:
            return list(self._data.get(str(chat_id)) or [])

    def append(self, chat_id: int | str, summary: TaskSummary) -> None:
        with self._lock:
            buf = self._data.setdefault(str(chat_id), [])
            buf.append(summary)
            del buf[:-MAX_PER_CHAT]
            self._save()

    def render_context(self, chat_id: int | str) -> str:
        """Format prior turns into a session_context block the agent loop
        understands. Returns "" if no history."""
        rows = self.history(chat_id)
        if not rows:
            return ""
        lines = ["RECENT SESSION CONTEXT (read-only, prior tasks in this chat):"]
        for r in rows:
            res = (r.result or "").replace("\n", " ")
            if len(res) > 140:
                res = res[:140] + "…"
            lines.append(f'  - "{r.task}" → {r.status}: {res}')
        lines.append("END SESSION CONTEXT")
        return "\n".join(lines)
