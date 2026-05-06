"""OpenAI Responses API via ChatGPT subscription OAuth.

Reuses the access_token Codex CLI stores in ``~/.codex/auth.json``. Hits the
``chatgpt.com/backend-api/codex/responses`` endpoint, which accepts ChatGPT
account auth (the public ``api.openai.com`` rejects this token, missing the
``model.request`` scope).

Constraints discovered by probing:
  - `instructions` field required
  - `store` must be false
  - `stream` must be true (SSE response)
  - allowed models: gpt-5.5, gpt-5.3-codex (others rejected as
    "not supported when using Codex with a ChatGPT account")
"""
from __future__ import annotations

import base64
import io
import json
import re
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from PIL import Image

_URL = "https://chatgpt.com/backend-api/codex/responses"

MODEL = "gpt-5.5"


@dataclass
class Prediction:
    x: int
    y: int
    method: str
    raw: str


def _tokens() -> tuple[str, str]:
    """Load ChatGPT OAuth tokens. Raises with a friendly message if the
    user hasn't logged in yet."""
    from .auth import load_tokens
    try:
        auth = load_tokens()
    except FileNotFoundError as e:
        raise RuntimeError(str(e)) from e
    tk = auth.get("tokens") or {}
    access, account_id = tk.get("access_token"), tk.get("account_id")
    if not access or not account_id:
        raise RuntimeError(
            "auth file exists but is missing access_token / account_id. "
            "Run `openseer auth login` to refresh."
        )
    return access, account_id


def _stream(payload: dict) -> str:
    """Backward-compat: return only the text. Use _stream_full to also
    capture per-event SSE log + usage metadata for trace analysis."""
    text, _events, _usage = _stream_full(payload)
    return text


def _stream_full(payload: dict, *, max_retries: int = 3,
                 on_delta=None) -> tuple[str, list[dict], dict]:
    """Stream and return (text, events, usage).

    Retries on HTTP 429 (rate limit) and 5xx with exponential backoff
    1s / 2s / 4s. Other errors propagate.

    `on_delta(text_so_far: str)` is called for each text delta as it
    arrives, allowing live UI of the model's output as it forms (the
    `thought` field is emitted first, so this surfaces the plan in
    real time before the action runs).
    """
    import time as _time

    access, account_id = _tokens()
    body = json.dumps(payload).encode()
    headers = {
        "Authorization": f"Bearer {access}",
        "Content-Type": "application/json",
        "chatgpt-account-id": account_id,
        "Accept": "text/event-stream",
        "OpenAI-Beta": "responses=v1",
    }

    def _do_one() -> tuple[str, list[dict], dict]:
        req = urllib.request.Request(_URL, data=body, method="POST", headers=headers)
        text = ""
        events: list[dict] = []
        usage: dict = {}
        with urllib.request.urlopen(req, timeout=120) as r:
            for line in r:
                s = line.decode().rstrip()
                if not s.startswith("data: "):
                    continue
                d = json.loads(s[6:])
                events.append(d)
                t = d.get("type")
                if t == "response.output_text.delta":
                    text += d.get("delta", "")
                    if on_delta is not None:
                        try:
                            on_delta(text)
                        except Exception:
                            pass
                elif t == "response.completed":
                    usage = (d.get("response") or {}).get("usage") or {}
                    break
                elif t == "response.failed":
                    raise RuntimeError(f"response.failed: {d}")
        return text, events, usage

    for attempt in range(max_retries + 1):
        try:
            return _do_one()
        except urllib.error.HTTPError as e:
            err_body = ""
            try:
                err_body = e.read().decode()[:400]
            except Exception:
                pass
            retryable = (e.code == 429) or (500 <= e.code < 600)
            if retryable and attempt < max_retries:
                wait = 2 ** attempt   # 1, 2, 4 seconds
                print(f"  [retry] HTTP {e.code} attempt {attempt+1}/{max_retries+1}; "
                      f"sleeping {wait}s. {err_body[:120]}")
                _time.sleep(wait)
                continue
            raise RuntimeError(f"HTTP {e.code}: {err_body}") from e
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            if attempt < max_retries:
                wait = 2 ** attempt
                print(f"  [retry] {type(e).__name__} attempt {attempt+1}/{max_retries+1}; "
                      f"sleeping {wait}s")
                _time.sleep(wait)
                continue
            raise

    raise RuntimeError(f"all {max_retries + 1} attempts exhausted")


def _data_url(img: Image.Image, quality: int = 85) -> str:
    """Encode image as JPEG (quality 85) — 3-4x smaller than PNG with
    no measurable loss for UI grounding tasks. Saves significant upload
    time and input token budget."""
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=quality, optimize=True)
    return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()


_COORD_RE = re.compile(r'"x"\s*:\s*(\d+)[^}]*"y"\s*:\s*(\d+)|(\d+)\s*,\s*(\d+)')


def predict_vision_json(img: Image.Image, target: str, model: str = MODEL) -> Prediction:
    payload = {
        "model": model,
        "instructions": "You output only JSON. No prose, no code fences, no explanations.",
        "input": [{"role": "user", "content": [
            {"type": "input_image", "image_url": _data_url(img)},
            {"type": "input_text", "text": (
                f"The screenshot is {img.width}x{img.height} pixels. "
                f"Locate this element on the screen: {target}\n"
                f'Return only: {{"x": <int>, "y": <int>}} '
                f"with the pixel coordinate of its center."
            )},
        ]}],
        "stream": True,
        "store": False,
        # Grounding doesn't need deep reasoning — low effort runs ~2-3x faster
        # and tokens are cheap enough that we don't need to be picky on quality.
        "reasoning": {"effort": "low"},
    }
    text = _stream(payload)
    m = _COORD_RE.search(text)
    if not m:
        raise RuntimeError(f"could not parse coords from: {text!r}")
    if m.group(1):
        return Prediction(int(m.group(1)), int(m.group(2)), f"openai_{model}", text)
    return Prediction(int(m.group(3)), int(m.group(4)), f"openai_{model}", text)
