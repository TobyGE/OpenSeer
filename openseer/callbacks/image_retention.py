"""Keep only the N most recent screenshots in the multi-turn input.

Older user messages (which represent past states) get their image replaced
with a one-line text summary so the model still has SOME context, just
not the pixels.

Two modes:
  - "drop":    just remove the image, keep whatever text was already there.
               Cheapest. Matches CUA's ImageRetentionCallback behaviour.
  - "summary": replace image with `[step N: image omitted; <text>]`.
               Slightly richer; relies on the existing user-msg text
               (which already says "step X action: …  result: …").
"""
from __future__ import annotations

from typing import Any

from .base import Callback


class ImageRetentionCallback(Callback):
    def __init__(self, n: int = 4, mode: str = "summary"):
        if mode not in ("drop", "summary"):
            raise ValueError(f"mode must be 'drop' or 'summary', got {mode!r}")
        self.n = n
        self.mode = mode

    def on_messages_built(self, ctx: dict[str, Any],
                          items: list[dict]) -> list[dict]:
        # collect indices of all user messages that carry an image
        img_user_indices: list[int] = []
        for idx, item in enumerate(items):
            if item.get("role") != "user":
                continue
            if any(c.get("type") == "input_image" for c in item.get("content", [])):
                img_user_indices.append(idx)

        if len(img_user_indices) <= self.n:
            return items

        # keep the LAST n image-bearing user messages; trim images from the rest
        keep_idx = set(img_user_indices[-self.n:])
        out: list[dict] = []
        for idx, item in enumerate(items):
            if idx in img_user_indices and idx not in keep_idx:
                # strip image from this user message
                new_content = []
                kept_text = ""
                for c in item["content"]:
                    if c["type"] == "input_image":
                        continue  # drop
                    new_content.append(c)
                    if c["type"] == "input_text":
                        kept_text = c.get("text", "")
                if self.mode == "summary" and not new_content:
                    # message had only image — fabricate a placeholder
                    new_content.append({"type": "input_text",
                                        "text": "[older state, image omitted]"})
                elif self.mode == "summary":
                    # Annotate existing text so the model knows the image
                    # was dropped on purpose
                    for c in new_content:
                        if c["type"] == "input_text":
                            c["text"] = "[image omitted from older turn] " + c["text"]
                            break
                out.append({"role": item["role"], "content": new_content})
            else:
                out.append(item)
        return out
