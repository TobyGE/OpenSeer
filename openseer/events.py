"""Typed task events emitted by the agent loop.

Any UI (REPL, future TUI / web) consumes the same stream:

    task_started      — the loop is about to begin
    step_started      — entering a new outer-loop iteration
    model_started     — about to call the LLM
    model_finished    — LLM returned; payload includes raw text + usage
    action_parsed     — ONE Action has been parsed from the response
    action_started    — about to run a producing action (click/bash/...)
    action_finished   — action completed; payload includes the result string
    safety_blocked    — SafetyCallback rejected an action
    step_recorded     — a Step has been appended to history
    task_finished     — the loop ended with status (done/fail/cap)
    task_failed       — the loop crashed with an exception

Events are the *unit of communication* between the agent core and any
observer. Callbacks now subscribe via ``on_event(ctx, event)``;
``on_step_recorded`` etc. are still supported for back-compat but
``on_event`` gets richer, finer-grained signal.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any


# Allowed event type names. Single source of truth so consumers can
# match safely with `if event.type == EventType.MODEL_STARTED`.
class EventType:
    TASK_STARTED    = "task_started"
    STEP_STARTED    = "step_started"
    PREP_PHASE      = "prep_phase"        # screen captured / AX dumped / etc.
    MODEL_STARTED   = "model_started"
    MODEL_DELTA     = "model_delta"       # streaming output text chunk
    MODEL_FINISHED  = "model_finished"
    ACTION_PARSED   = "action_parsed"
    ACTION_STARTED  = "action_started"
    ACTION_FINISHED = "action_finished"
    SAFETY_BLOCKED  = "safety_blocked"
    STEP_RECORDED   = "step_recorded"
    # Hand-off: the user pressed "换我" / Hold; agent is suspended,
    # waiting for the HOLD sentinel to disappear (or CANCEL to win).
    AGENT_HELD      = "agent_held"
    AGENT_RESUMED   = "agent_resumed"
    TASK_FINISHED   = "task_finished"
    TASK_FAILED     = "task_failed"
    # Post-run reflection signals. SKILL_PROPOSED fires when the
    # reflection pass identified a durable lesson and wrote a
    # proposed SKILL.md to the run dir, but is waiting for the user
    # to confirm before persisting. The GUI / voice orb shows a chip
    # offering Save / Discard / Preview, and replies with
    # `apply_skill` or `discard_skill` over the agentd WS. CLI users
    # still get the in-process input() prompt fallback when no event
    # bridge is wired up.
    SKILL_PROPOSED  = "skill_proposed"
    SKILL_APPLIED   = "skill_applied"
    SKILL_DISCARDED = "skill_discarded"


@dataclass
class TaskEvent:
    type: str
    timestamp: float = field(default_factory=time.time)
    step: int | None = None       # outer-loop step number (1-indexed) when applicable
    data: dict[str, Any] = field(default_factory=dict)

    def get(self, key: str, default: Any = None) -> Any:
        return self.data.get(key, default)
