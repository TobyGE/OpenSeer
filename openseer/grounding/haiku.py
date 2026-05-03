"""Grounder using Anthropic Claude Haiku 4.5 via PersonalMem's OAuth.

Same code path as our earlier vision_json experiments. Useful as a cheap
A/B comparator for GPT-5.5; we saw Haiku miss dense Dock icons by
~100px, so this is mostly for analysis, not production fallback.
"""
from __future__ import annotations

import time

from PIL import Image

from ..ground import predict_vision_json as haiku_vision_json
from .base import GroundingResult


class HaikuGrounder:
    name = "haiku"

    def predict(self, img: Image.Image, target: str) -> GroundingResult:
        t0 = time.time()
        p = haiku_vision_json(img, target)
        return GroundingResult(
            x=p.x, y=p.y, backend=self.name,
            raw=p.raw, elapsed_ms=int((time.time() - t0) * 1000),
        )
