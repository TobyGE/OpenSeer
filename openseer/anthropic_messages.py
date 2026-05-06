"""Anthropic Messages API client (OAuth via Claude Code subscription).

This is the parallel of `openai_chatgpt.py` for the Claude side. We
read the same OAuth token Claude Code stores in macOS Keychain under
service `Claude Code-credentials`, hit `api.anthropic.com/v1/messages`
with `anthropic-beta: oauth-2025-04-20`, and stream Server-Sent Events.

Usage from agent.py:
    text, events, usage = stream_full(payload, on_delta=...)

The `payload` shape we accept here is the OpenSeer-internal one (the
same one we built for OpenAI Responses) — adapt it to Anthropic's
shape inside `_to_anthropic_payload`.
"""
from __future__ import annotations

import json
import re
import subprocess
import time
import urllib.error
import urllib.request


MODEL = "claude-haiku-4-5-20251001"

_API_URL = "https://api.anthropic.com/v1/messages"
_KEYCHAIN_SERVICE = "Claude Code-credentials"
_OAUTH_BETA = "oauth-2025-04-20"
_API_VERSION = "2023-06-01"


# ───────────────────────────── auth ─────────────────────────────

def _load_oauth() -> dict:
    """Pull the Claude Code OAuth blob from the macOS Keychain.
    Raises RuntimeError if it isn't there (user must run Claude Code
    or `claude` CLI login first)."""
    try:
        r = subprocess.run(
            ["security", "find-generic-password",
             "-s", _KEYCHAIN_SERVICE, "-w"],
            capture_output=True, text=True, timeout=4,
        )
    except Exception as e:
        raise RuntimeError(f"keychain read failed: {e}") from e
    if r.returncode != 0:
        raise RuntimeError(
            "Claude Code OAuth not found in macOS Keychain "
            f"(service {_KEYCHAIN_SERVICE!r}). Sign in via the "
            "Claude desktop app or `claude` CLI first."
        )
    try:
        blob = json.loads(r.stdout.strip())
        return blob["claudeAiOauth"]
    except Exception as e:
        raise RuntimeError(f"keychain payload malformed: {e}") from e


def _access_token() -> str:
    """Get a usable access token, refreshing once if expiry is close.
    Currently we don't proactively refresh; if the token is rejected
    the caller bubbles the HTTP 401 — user is expected to re-login
    via Claude Code (refresh tokens belong to that app)."""
    o = _load_oauth()
    return o["accessToken"]


def token_status() -> dict:
    """For `openseer auth status` to print. Best-effort, never raises."""
    try:
        o = _load_oauth()
    except Exception as e:
        return {"present": False, "error": str(e)}
    expires = o.get("expiresAt", 0) / 1000.0
    return {
        "present": True,
        "subscription": o.get("subscriptionType", "?"),
        "scopes": o.get("scopes", []),
        "expires_at": expires,
        "expires_in_s": int(expires - time.time()),
    }


# ───────────────────────────── payload conversion ─────────────────

def _to_anthropic_payload(openseer_payload: dict) -> dict:
    """Convert the agent's internal payload shape (built for OpenAI
    Responses API) into Anthropic Messages API shape.

    Input shape:
        {
          "model": str,                # ignored, we override
          "instructions": str,         # → top-level `system`
          "input": [                   # → `messages`
            {"role": "user"|"assistant", "content": [
              {"type": "input_image", "image_url": "data:image/png;base64,..."},
              {"type": "input_text",  "text": "..."},
              {"type": "output_text", "text": "..."},   # assistant only
            ]}
          ],
          "stream": True, "store": False, "reasoning": {"effort": "low"},
        }
    """
    messages: list[dict] = []
    for it in openseer_payload.get("input", []):
        role = it.get("role")
        if role not in ("user", "assistant"):
            continue
        content_in = it.get("content", [])
        if isinstance(content_in, str):
            messages.append({"role": role, "content": content_in})
            continue
        blocks: list[dict] = []
        for p in content_in:
            t = (p or {}).get("type")
            if t in ("input_text", "output_text"):
                blocks.append({"type": "text", "text": p.get("text", "")})
            elif t == "input_image":
                url = p.get("image_url", "")
                # data URLs only — we never send remote URLs
                m = re.match(r"data:(image/[\w.+-]+);base64,(.+)$", url, re.S)
                if not m:
                    continue
                blocks.append({
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": m.group(1),
                        "data": m.group(2),
                    },
                })
        if blocks:
            messages.append({"role": role, "content": blocks})

    # Anthropic OAuth beta requires the system prompt to start with the
    # exact "Claude Code" prefix line, otherwise the request is rejected
    # with a 403. Prepend it; the rest is OpenSeer's own instructions.
    #
    # Claude was trained on a `<function_calls><invoke>...` XML schema
    # for tool use, so even when the prompt asks for JSON it likes to
    # emit prose + XML + a trailing JSON. We aggressively forbid that
    # and pin the output shape with an explicit example. Without this,
    # only the last JSON object is parsed (losing the chain Claude
    # actually intended), and the agent hits the step cap with no
    # progress.
    sys_prompt = openseer_payload.get("instructions") or ""
    forbid_xml = (
        "OUTPUT CONTRACT — read carefully, every turn:\n"
        "Your ENTIRE response MUST be ONE valid JSON object. NOTHING ELSE.\n"
        "Begin with `{` and end with `}`. The very first character of your\n"
        "response MUST be `{` — no whitespace, no newline, no text before it.\n"
        "\n"
        "ABSOLUTE BANS (silently dropped → run stalls):\n"
        "  - markdown code fences. NO ``` of any kind. NO ```json. NO ```.\n"
        "  - prose / commentary before, between, or after the JSON\n"
        "  - <function_calls>, <invoke>, <parameter> XML or any tool-use markup\n"
        "  - phrases like 'Let me ...', 'I can see ...', 'Perfect!', 'Now I will ...'\n"
        "  - the key `thinking` — the field name is `thought`, exactly\n"
        "\n"
        "FIELD RULES:\n"
        "  - `thought` (REQUIRED): one short sentence, ≤ 25 words. NOT a paragraph.\n"
        "    Start with the reflection token: [SUCCESS|INEFFECTIVE|REGRESSED|N/A]\n"
        "    Example: \"[SUCCESS] search results loaded. Next: click first 西游记.\"\n"
        "    NOT: long descriptions of what you see (the screenshot is enough).\n"
        "  - `action` XOR `actions` — one or the other.\n"
        "\n"
        "Single-action shape:\n"
        '  {"thought":"<≤25w>","action":"<name>", ...args}\n'
        "Chain shape (multiple deterministic moves):\n"
        '  {"thought":"<≤25w>","actions":[{"action":"...","..."}, ...]}\n'
        "\n"
        "CORRECT (copy this exact shape, brevity included):\n"
        '  {"thought":"[N/A] first turn. Next: open 微信读书.",'
        '"action":"open_app","app":"微信读书"}\n'
        "\n"
        "WRONG — every one of these will break parsing or stall:\n"
        '  ```json {"thought":"..."} ```            ← fences forbidden\n'
        '  Let me open it. {"thought":"..."}        ← prose forbidden\n'
        '  {"thinking":"...","action":"..."}        ← wrong key name\n'
        '  {"thought":"I can see the desktop with iTerm2 in front and several\n'
        '   windows visible including ...","action":"..."}  ← thought too long\n'
        "\n"
    )
    sys_prefixed = ("You are Claude Code, Anthropic's official CLI for Claude.\n\n"
                    + forbid_xml + sys_prompt)

    out: dict = {
        "model": MODEL,
        "max_tokens": 4096,
        "system": sys_prefixed,
        "messages": messages,
        "stream": True,
    }
    # Map effort → extended-thinking budget. low = off (cheapest).
    eff = (openseer_payload.get("reasoning") or {}).get("effort")
    budget = {"medium": 4000, "high": 16000}.get(eff)
    if budget:
        out["thinking"] = {"type": "enabled", "budget_tokens": budget}
    return out


# ───────────────────────────── streaming ─────────────────────────

def stream_full(openseer_payload: dict, *, max_retries: int = 3,
                on_delta=None) -> tuple[str, list[dict], dict]:
    """Stream and return (text, events, usage). Same return shape as
    `openseer.openai_chatgpt._stream_full` so the agent loop is
    provider-agnostic."""
    payload = _to_anthropic_payload(openseer_payload)
    body = json.dumps(payload).encode()

    def _do_one() -> tuple[str, list[dict], dict]:
        token = _access_token()
        req = urllib.request.Request(
            _API_URL, data=body, method="POST",
            headers={
                "Authorization": f"Bearer {token}",
                "anthropic-version": _API_VERSION,
                "anthropic-beta": _OAUTH_BETA,
                "Content-Type": "application/json",
                "Accept": "text/event-stream",
            },
        )
        text = ""
        events: list[dict] = []
        usage: dict = {}
        # Anthropic SSE: lines like `event: <name>\ndata: <json>\n\n`
        with urllib.request.urlopen(req, timeout=120) as r:
            for raw_line in r:
                s = raw_line.decode().rstrip()
                if not s.startswith("data: "):
                    continue
                payload_s = s[6:]
                if payload_s.strip() == "":
                    continue
                try:
                    d = json.loads(payload_s)
                except Exception:
                    continue
                events.append(d)
                t = d.get("type")
                if t == "content_block_delta":
                    delta = d.get("delta", {})
                    if delta.get("type") == "text_delta":
                        text += delta.get("text", "")
                        if on_delta is not None:
                            try:
                                on_delta(text)
                            except Exception:
                                pass
                elif t == "message_delta":
                    # Anthropic carries usage in the final message_delta
                    u = d.get("usage") or {}
                    if u:
                        usage = {
                            "input_tokens": u.get("input_tokens", 0),
                            "output_tokens": u.get("output_tokens", 0),
                        }
                elif t == "message_start":
                    msg = d.get("message", {})
                    u = msg.get("usage") or {}
                    if u:
                        usage = {
                            "input_tokens": u.get("input_tokens", 0),
                            "output_tokens": u.get("output_tokens", 0),
                        }
                elif t == "error":
                    raise RuntimeError(f"anthropic stream error: {d}")
                elif t == "message_stop":
                    break
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
                wait = 2 ** attempt
                print(f"  [retry] HTTP {e.code} attempt {attempt + 1}/{max_retries + 1}; "
                      f"sleeping {wait}s. {err_body[:120]}")
                time.sleep(wait)
                continue
            raise RuntimeError(f"HTTP {e.code}: {err_body}") from e
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            if attempt < max_retries:
                wait = 2 ** attempt
                print(f"  [retry] network {type(e).__name__} attempt "
                      f"{attempt + 1}/{max_retries + 1}; sleeping {wait}s")
                time.sleep(wait)
                continue
            raise

    raise RuntimeError("anthropic stream exhausted retries")
