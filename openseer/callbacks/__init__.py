"""Callback system for the openseer agent loop.

Inspired by trycua/cua's ComputerAgent. Decouples cross-cutting concerns
(image retention, trajectory saving, cost budget, …) from the core
observe→think→act loop.

Hooks (all sync; we have no async loop to schedule against):
  on_run_start(ctx)               — once at start; ctx has task, out_dir, model, system_prompt, history (empty)
  on_messages_built(ctx, items)   — mutate the multi-turn input array; return new list
  on_step_recorded(ctx, step)     — after a Step has been built and appended to history
  on_should_continue(ctx) -> bool — return False to abort the loop (budget exceeded etc.)
  on_run_end(ctx)                 — once at end with full history

`ctx` is a dict that the agent maintains; callbacks read/write fields on it.
Common keys: task, model, system_prompt, out_dir, history (list[Step]),
step_idx (current 1-indexed during a step).
"""
from .base import Callback
from .budget import BudgetCallback
from .image_retention import ImageRetentionCallback
from .trajectory import TrajectoryCallback

__all__ = ["Callback", "BudgetCallback", "ImageRetentionCallback", "TrajectoryCallback"]
