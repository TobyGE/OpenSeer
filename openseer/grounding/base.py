"""Grounder protocol: image + description → pixel (x, y)."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from PIL import Image


@dataclass
class GroundingResult:
    x: int
    y: int
    backend: str
    raw: str = ""           # raw model response, for debugging
    elapsed_ms: int = 0
    usage: dict | None = None


class Grounder(Protocol):
    """Resolve a textual element description to pixel coordinates in `img`."""

    name: str

    def predict(self, img: Image.Image, target: str) -> GroundingResult: ...
