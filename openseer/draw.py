"""Visualize predicted (x, y) on the screenshot."""
from __future__ import annotations

from PIL import Image, ImageDraw, ImageFont

_COLORS = {
    "computer_use":     (0, 200, 0),
    "vision_json":      (0, 120, 255),     # Haiku via Anthropic OAuth
    "openai_gpt-5.5":   (255, 140, 0),     # GPT-5.5 via ChatGPT OAuth
    "openai_gpt-5.3-codex": (200, 0, 200),
    "ground_truth":     (0, 0, 0),         # eyeballed truth
}


def annotate(img: Image.Image, marks: list[tuple[int, int, str, str]]) -> Image.Image:
    """marks: list of (x, y, method, label).

    Draws a 30px crosshair + filled dot + label per mark.
    """
    out = img.copy()
    draw = ImageDraw.Draw(out)
    try:
        font = ImageFont.truetype("/System/Library/Fonts/Supplemental/Arial Bold.ttf", 18)
    except OSError:
        font = ImageFont.load_default()

    for x, y, method, label in marks:
        color = _COLORS.get(method, (255, 0, 0))
        # crosshair
        draw.line([(x - 25, y), (x + 25, y)], fill=color, width=3)
        draw.line([(x, y - 25), (x, y + 25)], fill=color, width=3)
        # ring
        draw.ellipse([(x - 12, y - 12), (x + 12, y + 12)], outline=color, width=3)
        # dot
        draw.ellipse([(x - 3, y - 3), (x + 3, y + 3)], fill=color)
        # label box
        text = f"{method}: ({x},{y})\n{label}"
        bbox = draw.multiline_textbbox((0, 0), text, font=font, spacing=2)
        tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
        # place label to the right unless near right edge, then to the left
        lx = x + 30 if x + 30 + tw + 8 < out.width else x - 30 - tw - 8
        ly = max(0, y - th // 2)
        draw.rectangle([(lx - 4, ly - 4), (lx + tw + 4, ly + th + 4)], fill=(0, 0, 0))
        draw.multiline_text((lx, ly), text, fill=color, font=font, spacing=2)
    return out
