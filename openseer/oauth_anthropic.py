"""Claude / Anthropic OAuth login — implemented directly in OpenSeer.

Replaces the previous flow that shelled out to ``claude auth login``.
Claude's OAuth client doesn't register a localhost callback (unlike
Codex), so the flow is split into two steps:

  1. ``run_login_step1()`` opens the browser and returns the
     ``state`` + ``code_verifier`` the caller must hold onto.
  2. ``run_login_step2(code, verifier)`` exchanges the code the user
     pasted back for tokens and writes them to macOS Keychain in the
     ``Claude Code-credentials`` blob shape ``openseer.anthropic_messages``
     already reads.

The CLI subcommand ``openseer auth login --provider anthropic`` runs
both steps inline and prompts on stdin between them. The GUI can call
the two steps separately so it can render a "paste your code here"
text field instead of asking the user to bounce through a terminal.
"""
from __future__ import annotations

import base64
import hashlib
import json
import secrets
import subprocess
import sys
import time
import urllib.parse
import urllib.request
import webbrowser
from typing import Any

# Public OAuth client id Claude Code ships in its npm package.
CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"
AUTHORIZE_URL = "https://claude.com/cai/oauth/authorize"
TOKEN_URL = "https://platform.claude.com/v1/oauth/token"
# Anthropic only registers a hosted callback for this client; the
# success page surfaces the auth code to the user as text.
REDIRECT_URI = "https://platform.claude.com/oauth/code/callback"
SCOPES = "org:create_api_key user:profile user:inference"

KEYCHAIN_SERVICE = "Claude Code-credentials"


def _b64url(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode("ascii")


def _gen_pkce() -> tuple[str, str]:
    verifier = _b64url(secrets.token_bytes(32))
    challenge = _b64url(hashlib.sha256(verifier.encode("ascii")).digest())
    return verifier, challenge


def build_auth_url() -> tuple[str, str, str]:
    """Build the authorize URL and return (url, state, code_verifier).
    Caller must keep ``state`` and ``code_verifier`` for step 2."""
    verifier, challenge = _gen_pkce()
    state = _b64url(secrets.token_bytes(16))
    url = AUTHORIZE_URL + "?" + urllib.parse.urlencode({
        "code":                  "true",
        "client_id":             CLIENT_ID,
        "response_type":         "code",
        "redirect_uri":          REDIRECT_URI,
        "scope":                 SCOPES,
        "code_challenge":        challenge,
        "code_challenge_method": "S256",
        "state":                 state,
    })
    return url, state, verifier


def exchange_code(code: str, verifier: str, state: str | None = None,
                  expected_state: str | None = None) -> dict[str, Any]:
    """Exchange the code the user pasted for an access/refresh token
    pair. ``state`` is what the user pasted (may be empty if the
    success page didn't surface it); ``expected_state`` is what step
    1 generated. We only error on mismatch when both are present."""
    if state and expected_state and state != expected_state:
        raise RuntimeError("state mismatch — possible CSRF / wrong "
                           "browser tab.")
    body = json.dumps({
        "grant_type":    "authorization_code",
        "client_id":     CLIENT_ID,
        "code":          code,
        "redirect_uri":  REDIRECT_URI,
        "code_verifier": verifier,
        "state":         state or "",
    }).encode("utf-8")
    req = urllib.request.Request(
        TOKEN_URL, data=body, method="POST",
        headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        # Surface the body so the user sees what auth0 didn't like
        # (wrong code, expired challenge, etc.) instead of a bare 4xx.
        raw = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"token exchange HTTP {e.code}: {raw}") from e


def save_to_keychain(tokens: dict[str, Any]) -> None:
    """Write the token blob to macOS Keychain under
    ``Claude Code-credentials`` in the same JSON shape Claude Code
    itself stores, so ``openseer.anthropic_messages`` keeps working
    without changes."""
    expires_at_ms = int((time.time()
                         + (tokens.get("expires_in") or 3600)) * 1000)
    blob = {
        "claudeAiOauth": {
            "accessToken":      tokens.get("access_token", "") or "",
            "refreshToken":     tokens.get("refresh_token", "") or "",
            "expiresAt":        expires_at_ms,
            "scopes":           (tokens.get("scope") or "").split(),
            "subscriptionType": tokens.get("subscription_type") or "",
        }
    }
    payload = json.dumps(blob)
    # `security add-generic-password -U` updates the existing entry
    # if one is already present (otherwise it errors with -U missing).
    cmd = ["security", "add-generic-password",
           "-s", KEYCHAIN_SERVICE,
           "-a", KEYCHAIN_SERVICE,   # account name (Claude Code uses same)
           "-w", payload,
           "-U"]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(
            f"keychain write failed: {r.stderr.strip() or r.stdout.strip()}")


def run_login_start() -> int:
    """GUI step-1: open the browser and emit state+verifier so the
    GUI can hold them while it shows a "paste code" field. Prints
    a single JSON line on stdout with `url`, `state`, `verifier`,
    and `opened` (false when webbrowser.open returned False — the
    GUI is expected to surface the URL for manual copy). Always
    exits 0; the GUI decides whether to warn."""
    url, state, verifier = build_auth_url()
    opened = bool(webbrowser.open(url))
    print(json.dumps({"url": url, "state": state, "verifier": verifier,
                      "opened": opened}))
    return 0


def run_login_finish_from_stdin() -> int:
    """GUI step-2 with secrets read from stdin instead of argv —
    `ps`-visible argv would leak the auth code and code_verifier
    to other processes on the same machine. Stdin format: a single
    JSON object with keys `code`, `verifier`, optional `state`."""
    raw = sys.stdin.read()
    try:
        obj = json.loads(raw)
    except Exception as e:
        print(f"Couldn't parse stdin JSON: {e}")
        return 1
    code = obj.get("code") or ""
    verifier = obj.get("verifier") or ""
    state = obj.get("state") or None
    if not code or not verifier:
        print("Stdin JSON missing `code` and/or `verifier`.")
        return 1
    return run_login_finish(code=code, verifier=verifier,
                            expected_state=state)


def run_login_finish(code: str, verifier: str,
                     expected_state: str | None = None) -> int:
    """GUI step-2: exchange the code the user pasted in the GUI for
    tokens, write the Keychain blob, return 0 on success.

    We accept the code in either bare ("…") or "code#state" form —
    the success page sometimes surfaces both joined with `#`. State
    is optional but cross-checked against ``expected_state`` if both
    are present."""
    if "#" in code:
        c, _, returned_state = code.partition("#")
    else:
        c, returned_state = code, ""
    try:
        tokens = exchange_code(c.strip(), verifier,
                               state=returned_state.strip() or None,
                               expected_state=expected_state)
    except Exception as e:
        print(f"Token exchange failed: {e}")
        return 1
    if not tokens.get("access_token"):
        print(f"Token endpoint returned no access_token: {tokens!r}")
        return 1
    try:
        save_to_keychain(tokens)
    except Exception as e:
        print(f"Saved tokens but couldn't write Keychain: {e}")
        return 1
    print(f"✓ Signed in. Tokens saved to macOS Keychain "
          f"({KEYCHAIN_SERVICE}).")
    return 0


def run_login(timeout_s: int = 600, *, quiet: bool = False) -> int:
    """End-to-end CLI flow: open browser, prompt for code on stdin,
    exchange, write to Keychain. Returns 0 on success.

    Anthropic only registers a hosted callback for this OAuth client,
    so the user must paste the code shown on the success page back
    into the terminal. That requires an interactive stdin — when
    invoked from the macOS GUI's `Process` with piped stdin (codex
    P2), `input()` would EOF immediately. Detect that case and fall
    back to ``claude auth login``: the legacy CLI has its own browser
    UX that handles pipe-vs-tty correctly, so the GUI's Anthropic
    button keeps working until the GUI grows a paste-back dialog.
    """
    if not sys.stdin.isatty():
        if not quiet:
            print("Stdin isn't a terminal (likely launched from a GUI). "
                  "Falling back to `claude auth login` for the paste-"
                  "back step. Install via `npm install -g "
                  "@anthropic-ai/claude-code` if it isn't available.")
        from .setup_wizard import run_claude_login
        return run_claude_login()
    url, state, verifier = build_auth_url()
    if not quiet:
        print("→ Opening browser for Claude OAuth…")
        print(f"  If it doesn't open, paste this URL into a browser:\n"
              f"  {url}")
    webbrowser.open(url)
    if not quiet:
        print()
        print("After signing in, the page will show a long code like")
        print("    abc123...#xyz789")
        print("Copy the WHOLE thing (including the # if present) and")
        print("paste it below.")
        print()
    try:
        line = input("Paste code: ").strip()
    except (EOFError, KeyboardInterrupt):
        print("\nLogin cancelled.")
        return 130
    if not line:
        print("Empty input — aborted.")
        return 1
    # The success page sometimes formats it as "code#state". Split if
    # so; otherwise treat the whole thing as code.
    if "#" in line:
        code, _, returned_state = line.partition("#")
    else:
        code, returned_state = line, ""
    try:
        tokens = exchange_code(code.strip(), verifier,
                               state=returned_state.strip() or None,
                               expected_state=state)
    except Exception as e:
        print(f"Token exchange failed: {e}")
        return 1
    if not tokens.get("access_token"):
        print(f"Token endpoint returned no access_token: {tokens!r}")
        return 1
    try:
        save_to_keychain(tokens)
    except Exception as e:
        print(f"Saved tokens but couldn't write Keychain: {e}")
        return 1
    if not quiet:
        print("✓ Signed in. Tokens saved to macOS Keychain "
              f"({KEYCHAIN_SERVICE}).")
    return 0
