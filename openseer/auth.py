"""Authentication helpers for OpenSeer.

OpenSeer talks to the chatgpt.com codex backend using the access_token
that the official Codex CLI stores in ``~/.codex/auth.json``. The
upside: a Plus / Pro / Team subscriber can use OpenSeer without
ever creating an OpenAI API key — the OAuth login is handled by
Codex CLI's own browser flow.

This module exposes three things:

  ``load_tokens()``     — read & decode the saved auth.json
  ``token_status()``    — return a friendly summary (logged in / expired / plan tier)
  ``codex_cli_path()``  — find the codex CLI binary on PATH

A small CLI lives in ``openseer.cli`` (``openseer auth status`` /
``openseer auth login``) that calls these helpers.
"""
from __future__ import annotations

import base64
import json
import os
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

AUTH_FILE = Path.home() / ".codex" / "auth.json"


@dataclass
class TokenStatus:
    has_file: bool
    auth_mode: Optional[str] = None
    plan_type: Optional[str] = None       # 'free' / 'plus' / 'pro' / 'team' / ...
    account_id: Optional[str] = None
    expires_at: Optional[int] = None      # epoch seconds (best-effort, decoded from JWT)
    expired: bool = False
    error: Optional[str] = None

    def summary(self) -> str:
        if self.error:
            return f"❌ {self.error}"
        if not self.has_file:
            return ("❌ Not logged in. Run `openseer auth login` "
                    "(requires Codex CLI installed).")
        bits = [f"auth_mode={self.auth_mode}"]
        if self.plan_type:
            bits.append(f"plan={self.plan_type}")
        if self.account_id:
            bits.append(f"account={self.account_id[:8]}…")
        if self.expires_at:
            remain = self.expires_at - int(time.time())
            if remain > 0:
                hrs = remain / 3600
                bits.append(f"expires_in={hrs:.1f}h")
            else:
                bits.append("EXPIRED")
        flag = "⚠️  EXPIRED" if self.expired else "✅ logged in"
        return f"{flag} — {' '.join(bits)}"


def _decode_jwt_payload(jwt: str) -> dict:
    parts = jwt.split(".")
    if len(parts) != 3:
        return {}
    payload = parts[1]
    payload += "=" * (-len(payload) % 4)
    try:
        return json.loads(base64.urlsafe_b64decode(payload))
    except Exception:
        return {}


def load_tokens() -> dict:
    """Read ~/.codex/auth.json. Raises FileNotFoundError if missing."""
    if not AUTH_FILE.exists():
        raise FileNotFoundError(
            f"{AUTH_FILE} not found. OpenSeer needs Codex CLI's OAuth "
            f"token. Install Codex CLI and run `openseer auth login` "
            f"(or `codex login`)."
        )
    return json.loads(AUTH_FILE.read_text())


def token_status() -> TokenStatus:
    """Inspect saved tokens without making any network calls. Best-effort."""
    if not AUTH_FILE.exists():
        return TokenStatus(has_file=False)
    try:
        auth = json.loads(AUTH_FILE.read_text())
    except Exception as e:
        return TokenStatus(has_file=True,
                           error=f"can't parse {AUTH_FILE}: {e}")

    tokens = auth.get("tokens") or {}
    access = tokens.get("access_token") or ""
    payload = _decode_jwt_payload(access) if access.count(".") == 2 else {}
    auth_block = payload.get("https://api.openai.com/auth") or {}
    exp = payload.get("exp")
    expired = bool(exp and exp < time.time())
    return TokenStatus(
        has_file=True,
        auth_mode=auth.get("auth_mode"),
        plan_type=auth_block.get("chatgpt_plan_type"),
        account_id=tokens.get("account_id"),
        expires_at=exp,
        expired=expired,
    )


def codex_cli_path() -> Optional[str]:
    """Return path to the codex binary, or None if not installed."""
    return shutil.which("codex")


def run_codex_login() -> int:
    """Spawn `codex login` in the foreground (interactive). Returns exit code.

    Codex CLI handles the browser dance + saves tokens into
    ~/.codex/auth.json, which OpenSeer then reads.
    """
    bin_ = codex_cli_path()
    if not bin_:
        print(
            "❌ Codex CLI not found on PATH.\n\n"
            "OpenSeer reuses Codex CLI's OAuth tokens, so you need it\n"
            "installed once. Install with:\n\n"
            "    npm install -g @openai/codex\n\n"
            "or follow https://github.com/openai/codex.\n\n"
            "After installation, rerun `openseer auth login`."
        )
        return 127
    print(f"→ running {bin_} login")
    return subprocess.run([bin_, "login"]).returncode


def run_codex_logout() -> int:
    bin_ = codex_cli_path()
    if not bin_:
        # Fallback: just delete the file
        if AUTH_FILE.exists():
            AUTH_FILE.unlink()
            print(f"removed {AUTH_FILE} (codex CLI not installed)")
            return 0
        print("nothing to do — already logged out")
        return 0
    return subprocess.run([bin_, "logout"]).returncode
