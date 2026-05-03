"""Base Callback class.

Override only the hooks you need; default impls are no-ops (or identity
for `on_messages_built`).
"""
from __future__ import annotations

from typing import Any


class Callback:
    """Lifecycle hooks for the agent loop. Override what you need."""

    name: str = ""    # for log lines; defaults to class name if empty

    @property
    def label(self) -> str:
        return self.name or type(self).__name__

    def on_run_start(self, ctx: dict[str, Any]) -> None:
        """Called once at the start of run(), before any steps.
        ctx contains: task, model, system_prompt, out_dir, history=[]
        """

    def on_messages_built(self, ctx: dict[str, Any],
                          items: list[dict]) -> list[dict]:
        """Called after the agent built the multi-turn input array, before
        the API call. Return the (possibly modified) array.

        Use this for image retention, prompt-instruction prepending,
        PII redaction, etc.
        """
        return items

    def on_step_recorded(self, ctx: dict[str, Any], step: Any) -> None:
        """Called after a Step has been appended to ctx['history']."""

    def on_should_continue(self, ctx: dict[str, Any]) -> bool:
        """Called before each new step. Return False to stop the loop."""
        return True

    def on_run_end(self, ctx: dict[str, Any]) -> None:
        """Called once at the end of run() with the full history."""
