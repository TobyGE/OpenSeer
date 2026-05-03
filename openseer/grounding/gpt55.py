"""Grounder that asks GPT-5.5 (via ChatGPT subscription OAuth) for (x, y)
of a described element using the same vision_json path that powers our
quick-test harness.

This is the default and matches what the agent loop has been doing
all along — but now factored out so we can swap to other backends.
"""
from __future__ import annotations

import time

from PIL import Image

from ..openai_chatgpt import predict_vision_json
from .base import GroundingResult


class GPT55Grounder:
    name = "gpt55"

    def predict(self, img: Image.Image, target: str) -> GroundingResult:
        t0 = time.time()
        p = predict_vision_json(img, target)
        return GroundingResult(
            x=p.x, y=p.y, backend=self.name,
            raw=p.raw, elapsed_ms=int((time.time() - t0) * 1000),
        )
