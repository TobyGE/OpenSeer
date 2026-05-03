"""Ask Claude to locate an element and return (x, y).

Two paths:

  1. computer_use: declare the `computer_20251124` tool with the screenshot's
     own resolution and watch for a `left_click` tool_use block — the model
     drops coordinates in the same coordinate space we sent. This is the
     "real" grounding path used by Claude's computer use.

  2. vision_json: plain vision message, ask the model to return JSON
     `{"x":..,"y":..}`. This is the naive baseline most articles use; we
     run it for comparison.

Auth: reuse PersonalMem's Claude.com OAuth token from
``~/.guardclaw/oauth-tokens.json`` (or ``~/.personalmem/...``).
"""
from __future__ import annotations

import base64
import io
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from PIL import Image

_TOKEN_CANDIDATES = [
    Path.home() / ".personalmem" / "oauth-tokens.json",
    Path.home() / ".guardclaw" / "oauth-tokens.json",
]
_BASE_URL = "https://api.anthropic.com/v1"
_TOKEN_URL = "https://platform.claude.com/v1/oauth/token"
_CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"

MODEL_VISION = "claude-haiku-4-5-20251001"   # works for vision_json
MODEL_TOOL   = "claude-opus-4-5"             # required for computer_20251124 tool
# (Haiku 4.5 doesn't support the computer-use tool; only Opus 4.5+/Sonnet 4.6+/Opus 4.6+/4.7.)


@dataclass
class Prediction:
    x: int
    y: int
    method: str       # "computer_use" or "vision_json"
    raw: str          # raw text/tool input for debugging


def _token_path() -> Path:
    for p in _TOKEN_CANDIDATES:
        if p.exists():
            return p
    raise RuntimeError(f"No OAuth token file found in {_TOKEN_CANDIDATES}")


def _load_tokens() -> dict[str, Any]:
    return json.loads(_token_path().read_text())


def _save_tokens(tokens: dict[str, Any]) -> None:
    p = _token_path()
    p.write_text(json.dumps(tokens, indent=2))
    try:
        p.chmod(0o600)
    except OSError:
        pass


def _refresh(refresh_token: str) -> dict[str, Any]:
    body = urllib.parse.urlencode({
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        "client_id": _CLIENT_ID,
    }).encode()
    req = urllib.request.Request(
        _TOKEN_URL, data=body, method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode())


def _is_expired(tok: dict[str, Any]) -> bool:
    saved = tok.get("savedAt") or 0
    expires = tok.get("expires_in") or 0
    if not saved or not expires:
        return False
    return (time.time() * 1000) > (saved + expires * 1000 - 60_000)


def _access_token() -> str:
    tokens = _load_tokens()
    tok = tokens.get("claude") or {}
    access = tok.get("access_token")
    refresh = tok.get("refresh_token")
    if not access:
        raise RuntimeError("No access_token under provider 'claude'")
    if _is_expired(tok) and refresh:
        new = _refresh(refresh)
        tok = {**tok, **new, "savedAt": int(time.time() * 1000)}
        tokens["claude"] = tok
        _save_tokens(tokens)
        access = tok["access_token"]
    return access


def _force_refresh() -> str:
    tokens = _load_tokens()
    tok = tokens.get("claude") or {}
    refresh = tok.get("refresh_token")
    if not refresh:
        raise RuntimeError("No refresh_token")
    new = _refresh(refresh)
    tok = {**tok, **new, "savedAt": int(time.time() * 1000)}
    tokens["claude"] = tok
    _save_tokens(tokens)
    return tok["access_token"]


def _post(payload: dict[str, Any], betas: list[str]) -> dict[str, Any]:
    body = json.dumps(payload).encode()
    headers = {
        "Content-Type": "application/json",
        "anthropic-version": "2023-06-01",
        "anthropic-beta": ",".join(betas),
    }

    def _do(token: str) -> dict[str, Any]:
        req = urllib.request.Request(
            f"{_BASE_URL}/messages", data=body, method="POST",
            headers={**headers, "Authorization": f"Bearer {token}"},
        )
        with urllib.request.urlopen(req, timeout=120) as r:
            return json.loads(r.read().decode())

    tok = _access_token()
    try:
        return _do(tok)
    except urllib.error.HTTPError as e:
        err_text = e.read().decode("utf-8", errors="replace") if hasattr(e, "read") else ""
        if e.code == 401:
            return _do(_force_refresh())
        raise RuntimeError(f"HTTP {e.code}: {err_text}") from e


def _b64_png(img: Image.Image) -> str:
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode("ascii")


def _image_block(img: Image.Image) -> dict[str, Any]:
    return {
        "type": "image",
        "source": {
            "type": "base64",
            "media_type": "image/png",
            "data": _b64_png(img),
        },
    }


def predict_computer_use(img: Image.Image, target: str) -> Prediction:
    """Ask Claude via the computer_20251124 tool to click the target element."""
    payload = {
        "model": MODEL_TOOL,
        "max_tokens": 1024,
        "tools": [
            {
                "type": "computer_20251124",
                "name": "computer",
                "display_width_px": img.width,
                "display_height_px": img.height,
                "display_number": 1,
            }
        ],
        "messages": [
            {
                "role": "user",
                "content": [
                    _image_block(img),
                    {
                        "type": "text",
                        "text": (
                            f"This is the current screen. Please call the computer tool with "
                            f"a `left_click` action targeting: {target}. "
                            f"Do not take a screenshot first — the screen is provided. "
                            f"Just emit one `left_click` tool call with the coordinate."
                        ),
                    },
                ],
            }
        ],
    }
    data = _post(payload, betas=["computer-use-2025-11-24", "oauth-2025-04-20"])
    for block in data.get("content") or []:
        if block.get("type") == "tool_use" and block.get("name") == "computer":
            inp = block.get("input") or {}
            if inp.get("action") == "left_click" and "coordinate" in inp:
                x, y = inp["coordinate"]
                return Prediction(int(x), int(y), "computer_use", json.dumps(inp))
    raise RuntimeError(f"No left_click in response: {json.dumps(data)[:600]}")


_COORD_RE = re.compile(r'"x"\s*:\s*(\d+)[^}]*"y"\s*:\s*(\d+)|(\d+)\s*,\s*(\d+)')


def predict_vision_json(img: Image.Image, target: str) -> Prediction:
    """Naive baseline: ask the model in plain prose to return JSON coordinates."""
    payload = {
        "model": MODEL_VISION,
        "max_tokens": 256,
        "messages": [
            {
                "role": "user",
                "content": [
                    _image_block(img),
                    {
                        "type": "text",
                        "text": (
                            f"The screenshot is {img.width}x{img.height} pixels. "
                            f"Locate this element on the screen: {target}\n"
                            f'Return ONLY a JSON object: {{"x": <int>, "y": <int>}} '
                            f"with the pixel coordinate of its center. No other text."
                        ),
                    },
                ],
            }
        ],
    }
    data = _post(payload, betas=["oauth-2025-04-20"])
    text = ""
    for block in data.get("content") or []:
        if block.get("type") == "text":
            text += block.get("text") or ""
    m = _COORD_RE.search(text)
    if not m:
        raise RuntimeError(f"Could not parse coords from response: {text!r}")
    if m.group(1):
        return Prediction(int(m.group(1)), int(m.group(2)), "vision_json", text)
    return Prediction(int(m.group(3)), int(m.group(4)), "vision_json", text)
