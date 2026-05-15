"""Token-budget enforcement.

The Codex backend doesn't return a `response_cost` field, so we estimate
cost from token counts using rough public pricing. Cap is applied across
the entire run; trip → on_should_continue returns False → loop ends.
"""
from __future__ import annotations

from typing import Any

from .base import Callback


# Rough public $/1M-token list prices, grouped by family. Used ONLY
# for the advisory `[budget] est=$…` line — token caps in
# max_input_tokens / max_output_tokens are the real ceiling. Numbers
# are approximate (Anthropic / OpenAI both change list prices
# without a versioning story) but get the order of magnitude right,
# which is what matters for a "noticed I burned 50× more than usual"
# signal. Update when adding a new model.
_PRICES_PER_M: dict[str, tuple[float, float]] = {
    # (input, output)
    "opus":    (15.0, 75.0),    # opus-4-x — premium tier
    "sonnet":  ( 3.0, 15.0),    # sonnet-4-x — mid tier
    "haiku":   ( 1.0,  5.0),    # haiku-4-x — cheap tier
    "gpt-5":   ( 5.0, 15.0),    # gpt-5.5 / gpt-5.3-codex — rough
}
# Fallback used when we can't match the model name. Picked to lean
# pessimistic so a printed $est is unlikely to under-report.
_PRICE_FALLBACK: tuple[float, float] = (5.0, 20.0)


def _price_for_model(model: str | None) -> tuple[float, float]:
    """Return (input_per_M_usd, output_per_M_usd) for a model id.

    Match by substring of the family token (e.g. `claude-opus-4-7`
    → `opus`). Keeps us correct across minor releases without a
    list to maintain. Unknown → conservative fallback so the
    [budget] line never silently under-estimates a new model.
    """
    if not model:
        return _PRICE_FALLBACK
    m = model.lower()
    for needle, prices in _PRICES_PER_M.items():
        if needle in m:
            return prices
    return _PRICE_FALLBACK


class BudgetCallback(Callback):
    def __init__(self, max_input_tokens: int = 200_000,
                 max_output_tokens: int = 20_000,
                 cost_per_m_input: float | None = None,
                 cost_per_m_output: float | None = None,
                 model: str | None = None,
                 verbose: bool = True):
        # Per-call overrides take precedence; otherwise fall back to
        # the per-family list above keyed on the active model.
        family_in, family_out = _price_for_model(model)
        if cost_per_m_input is None:
            cost_per_m_input = family_in
        if cost_per_m_output is None:
            cost_per_m_output = family_out
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
