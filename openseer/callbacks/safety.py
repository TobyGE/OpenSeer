"""Stop the agent before it does something the user can't undo.

The model has a `bash` tool now and can also click on dangerous UI
buttons. This callback runs after the model emits an action but before
the executor runs it. If the action matches a danger pattern, we
either:

  (a) `block`   → return a synthetic step result; never executes
  (b) `confirm` → prompt the user y/n in the terminal before running

Patterns are split by tool. Adding new ones is just appending to the
lists below; no agent-loop changes needed.

This is a deliberately small, opinionated allow-policy. It is NOT a
sandbox or capability system — that's a separate concern. Treat it
like a smoke alarm: catches the obvious cases, doesn't cover every
edge.
"""
from __future__ import annotations

import re
from typing import Any

from .base import Callback


# Compiled regex patterns. Match against the full bash `cmd` string.
_BASH_DANGER: list[tuple[re.Pattern, str]] = [
    (re.compile(r"\brm\s+(-[a-zA-Z]*[rRfF]|--recursive|--force)"),
        "rm with recursive/force flag"),
    (re.compile(r"\bsudo\b"), "sudo"),
    (re.compile(r"\bmkfs\b"), "filesystem format (mkfs)"),
    (re.compile(r"\bdd\s+if="), "dd raw write"),
    (re.compile(r">\s*/dev/(sd|disk|nvme)"), "raw device write"),
    (re.compile(r"curl[^|]+\|\s*sh"), "curl piped to shell"),
    (re.compile(r"wget[^|]+\|\s*sh"), "wget piped to shell"),
    (re.compile(r"\bchmod\s+(-R\s+)?[0-7]*7[0-7]{2}\b.*\s/(?:\s|$)"),
        "wide chmod on /"),
    (re.compile(r"\bgit\s+push\s+(-f|--force)"), "git force push"),
    (re.compile(r"\bgit\s+reset\s+--hard"), "git reset --hard"),
    (re.compile(r"\brm\s+-rf?\s+(/|~|\$HOME)"), "rm -rf on home or root"),
    (re.compile(r"\bshutdown\b|\breboot\b|\bhalt\b"), "system power command"),
]

# Click-target words that imply destructive UI buttons. Best-effort:
# we only have the action's textual `target`/`reason`/`thought`, not OCR
# of the actual button. So this catches descriptive "click Delete" but
# can't catch a click on coords (x,y) where (x,y) lands on a Delete
# button without a description.
_CLICK_DANGER_WORDS = (
    "delete account", "delete forever", "empty trash",
    "drop database", "format disk", "uninstall",
    "send payment", "confirm purchase", "transfer funds",
    "factory reset", "erase all",
)


class SafetyCallback(Callback):
    """Inspect each Action before execute() runs it. Mode = block | confirm.

    We hook via on_messages_built because that's the only point we run
    around the agent loop. To intercept actions, we set a flag on ctx
    that the agent loop reads. (See agent.py: _check_safety call.)
    """

    name = "Safety"

    def __init__(self, mode: str = "confirm"):
        if mode not in ("block", "confirm", "log"):
            raise ValueError(f"SafetyCallback mode must be block|confirm|log, got {mode}")
        self.mode = mode

    # The agent loop calls this directly (not through a Callback hook) so
    # we expose it as a regular method. Returns (ok, reason).
    def check(self, action: Any) -> tuple[bool, str | None]:
        reason = self._classify(action)
        if reason is None:
            return True, None
        return False, reason

    def _classify(self, action: Any) -> str | None:
        if action.name == "bash":
            cmd = (action.cmd or "").strip()
            for pat, label in _BASH_DANGER:
                if pat.search(cmd):
                    return f"bash danger: {label}"
        if action.name == "click":
            blob = " ".join(filter(None, [action.target, action.thought, action.reason])).lower()
            for word in _CLICK_DANGER_WORDS:
                if word in blob:
                    return f"click danger keyword: {word!r}"
        return None
