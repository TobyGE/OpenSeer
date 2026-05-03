"""Pluggable grounding backends.

A Grounder takes (image, target_description) and returns pixel (x, y).
The agent's planner emits descriptions; this layer resolves them to
coordinates. Swap backends to A/B test grounding accuracy or to fall
back from one model to another when clicks misfire.
"""
from .base import Grounder, GroundingResult
from .gpt55 import GPT55Grounder
from .haiku import HaikuGrounder

# Registry for CLI / config-by-name selection.
REGISTRY: dict[str, type[Grounder]] = {
    "gpt55":  GPT55Grounder,
    "haiku":  HaikuGrounder,
}


def make(name: str) -> Grounder:
    if name not in REGISTRY:
        raise ValueError(f"unknown grounder {name!r}. available: {list(REGISTRY)}")
    return REGISTRY[name]()


__all__ = ["Grounder", "GroundingResult", "GPT55Grounder", "HaikuGrounder",
           "REGISTRY", "make"]
