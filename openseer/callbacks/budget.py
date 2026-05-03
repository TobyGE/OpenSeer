"""Token-budget enforcement.

The Codex backend doesn't return a `response_cost` field, so we estimate
cost from token counts using rough public pricing. Cap is applied across
the entire run; trip → on_should_continue returns False → loop ends.
"""
from __future__ import annotations

from typing import Any

from .base import Callback


# very rough $/1M token estimates; update if model changes.
# Used only for advisory cost printout — the real ceiling is `max_tokens`.
_DEFAULT_COST_PER_M_INPUT = 0.50
_DEFAULT_COST_PER_M_OUTPUT = 4.00


class BudgetCallback(Callback):
    def __init__(self, max_input_tokens: int = 200_000,
                 max_output_tokens: int = 20_000,
                 cost_per_m_input: float = _DEFAULT_COST_PER_M_INPUT,
                 cost_per_m_output: float = _DEFAULT_COST_PER_M_OUTPUT,
                 verbose: bool = True):
        self.max_input  = max_input_tokens
        self.max_output = max_output_tokens
        self.cost_in   = cost_per_m_input
        self.cost_out  = cost_per_m_output
        self.verbose = verbose

    def _totals(self, history) -> tuple[int, int]:
        ti = to = 0
        for s in history:
            if not s.usage:
                continue
            ti += s.usage.get("input_tokens", 0) or 0
            to += s.usage.get("output_tokens", 0) or 0
        return ti, to

    def on_step_recorded(self, ctx, step) -> None:
        ti, to = self._totals(ctx["history"])
        if self.verbose:
            est = ti / 1e6 * self.cost_in + to / 1e6 * self.cost_out
            print(f"  [budget] tokens used: in={ti}/{self.max_input}  "
                  f"out={to}/{self.max_output}  est=${est:.4f}")

    def on_should_continue(self, ctx) -> bool:
        ti, to = self._totals(ctx["history"])
        if ti >= self.max_input:
            print(f"  [budget] STOP — input tokens {ti} >= {self.max_input}")
            return False
        if to >= self.max_output:
            print(f"  [budget] STOP — output tokens {to} >= {self.max_output}")
            return False
        return True
