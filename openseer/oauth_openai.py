"""ChatGPT OAuth login — implemented directly in OpenSeer.

Replaces the previous flow that shelled out to ``codex login`` (which
required the user to install the Codex CLI separately). Uses the
same OAuth client id Codex publishes in its npm distribution; the
result is written to ``~/.codex/auth.json`` in the exact shape
``openseer.auth`` already expects.

Why we can use Codex's client id: it's a public PKCE OAuth client,
not a confidential one. The id is embedded in every Codex CLI
binary that ships from npm. Reusing it lets a Plus/Pro/Team
subscriber sign in without API keys, exactly the value Codex CLI
provides.

Reference for endpoints: ``https://auth.openai.com/.well-known/openid-configuration``.
Scopes mirror what an active Codex session shows in its access_token's
``scp`` claim.
"""
from __future__ import annotations

import base64
import hashlib
import http.server
import json
import os
import secrets
import socket
import threading
import time
import urllib.parse
import urllib.request
import webbrowser
from pathlib import Path
from typing import Any

# Public OAuth client id — same one Codex CLI ships in its npm pkg.
CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
# Authorize lives on auth.openai.com but token redemption is at the
# underlying auth0 tenant (auth0.openai.com). This split host is
# what `https://auth.openai.com/.well-known/openid-configuration`
# advertises and what Codex CLI itself uses; it's not a typo.
AUTHORIZE_URL = "https://auth.openai.com/authorize"
TOKEN_URL = "https://auth0.openai.com/oauth/token"
# Scopes a logged-in Codex session shows in its access_token.scp.
# `api.connectors.*` are the codex-backend-API scopes — without
# them the access_token won't be accepted by chatgpt.com/backend-api.
SCOPES = ("openid profile email offline_access "
          "api.connectors.read api.connectors.invoke")
# Codex registers `localhost:1455` as its primary callback URL with
# auth0. The Codex CLI source mentions a "registered fallback port"
# for the case when 1455 is busy; the surface area we get from auth0
# accepts at least 1455 and the alt slots Codex registers for the
# same client. We try the known-good 1455 first, then a small range
# of fallbacks before giving up.
CALLBACK_PORT = 1455
FALLBACK_PORTS = [1456, 1457, 1458]
CALLBACK_PATH = "/auth/callback"

AUTH_FILE = Path.home() / ".codex" / "auth.json"


def _b64url(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode("ascii")


def _gen_pkce() -> tuple[str, str]:
    verifier = _b64url(secrets.token_bytes(32))
    challenge = _b64url(hashlib.sha256(verifier.encode("ascii")).digest())
    return verifier, challenge


def _decode_jwt_payload(token: str) -> dict[str, Any]:
    parts = token.split(".")
    if len(parts) != 3:
        return {}
    pad = parts[1] + "=" * ((4 - len(parts[1]) % 4) % 4)
    try:
        return json.loads(base64.urlsafe_b64decode(pad))
    except Exception:
        return {}


def _check_port_available(port: int) -> bool:
    """Cheap probe — try to bind both stacks. We don't actually use
    the socket here; the real server binds in its own constructor."""
    for family, addr in ((socket.AF_INET, "127.0.0.1"),
                         (socket.AF_INET6, "::1")):
        try:
            s = socket.socket(family, socket.SOCK_STREAM)
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            s.bind((addr, port))
            s.close()
        except OSError:
            return False
    return True


class _LoopbackV4Server(http.server.HTTPServer):
    address_family = socket.AF_INET


class _LoopbackV6Server(http.server.HTTPServer):
    address_family = socket.AF_INET6

    def server_bind(self) -> None:
        # IPV6_V6ONLY=1 keeps this socket strictly IPv6-loopback so
        # we don't accidentally listen on dual-stack everything
        # (codex P2: binding `::` w/ V6ONLY=0 exposes the callback to
        # the LAN and trips firewall prompts).
        self.socket.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
        self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        super().server_bind()


def _start_loopback_listeners(port: int, handler):
    """Spin up two HTTPServers (127.0.0.1 + ::1) sharing one handler.
    We need both so the browser's `localhost` lookup reaches us
    regardless of /etc/hosts family order, while staying off the
    LAN. Returns the two server objects so the caller can shut
    them down on completion."""
    v4 = _LoopbackV4Server(("127.0.0.1", port), handler)
    v6 = _LoopbackV6Server(("::1", port), handler)
    threading.Thread(target=v4.serve_forever, daemon=True).start()
    threading.Thread(target=v6.serve_forever, daemon=True).start()
    return v4, v6


_SUCCESS_HTML = """<!doctype html>
<html><head><meta charset="utf-8"><title>OpenSeer login complete</title>
<style>body{font-family:-apple-system,BlinkMacSystemFont,sans-serif;
max-width:520px;margin:80px auto;padding:0 24px;color:#111}
h1{font-size:22px}p{color:#555;line-height:1.5}</style></head>
<body><h1>✓ Sign-in complete</h1>
<p>You can close this tab and return to OpenSeer.</p></body></html>
"""

_ERROR_HTML = """<!doctype html>
<html><head><meta charset="utf-8"><title>OpenSeer login failed</title>
<style>body{font-family:-apple-system,BlinkMacSystemFont,sans-serif;
max-width:520px;margin:80px auto;padding:0 24px;color:#111}
h1{font-size:22px;color:#c00}p{color:#555;line-height:1.5}
code{background:#f4f4f4;padding:2px 6px;border-radius:4px}</style></head>
<body><h1>✗ Sign-in failed</h1>
<p>__DETAIL__</p>
<p>Switch back to OpenSeer to retry.</p></body></html>
"""


def _error_page(detail: str) -> str:
    # Plain string substitution — `_ERROR_HTML` contains literal CSS
    # curly braces that would explode str.format() with KeyError. The
    # error path can't risk that since it's the path that needs to
    # actually report what went wrong (codex P2).
    return _ERROR_HTML.replace("__DETAIL__", detail)


def _build_callback_handler(expected_state: str,
                            captured: dict[str, Any]):
    class _Handler(http.server.BaseHTTPRequestHandler):
        # silence access log — login flow shouldn't spam stdout.
        def log_message(self, fmt: str, *args: Any) -> None:
            return

        def do_GET(self) -> None:                          # noqa: N802
            parsed = urllib.parse.urlparse(self.path)
            if parsed.path != CALLBACK_PATH:
                self._html(404, _error_page("Unknown path."))
                return
            qs = urllib.parse.parse_qs(parsed.query)
            err = qs.get("error", [None])[0]
            if err:
                desc = qs.get("error_description", [""])[0]
                self._html(400, _error_page(
                    f"OAuth provider returned <code>{err}</code>"
                    + (f": {desc}" if desc else "")))
                captured["error"] = err
                return
            code = qs.get("code", [None])[0]
            got_state = qs.get("state", [None])[0]
            if got_state != expected_state:
                self._html(400, _error_page(
                    "State mismatch — possible CSRF."))
                captured["error"] = "state_mismatch"
                return
            if not code:
                self._html(400, _error_page(
                    "No authorization code in callback."))
                captured["error"] = "missing_code"
                return
            self._html(200, _SUCCESS_HTML)
            captured["code"] = code

        def _html(self, code: int, body: str) -> None:
            data = body.encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

    return _Handler


def _exchange_code(code: str, verifier: str,
                   redirect_uri: str) -> dict[str, Any]:
    body = urllib.parse.urlencode({
        "grant_type": "authorization_code",
        "client_id": CLIENT_ID,
        "code": code,
        "redirect_uri": redirect_uri,
        "code_verifier": verifier,
    }).encode("ascii")
    req = urllib.request.Request(
        TOKEN_URL, data=body, method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read())


def _save_auth(tokens: dict[str, Any]) -> None:
    """Write tokens into ~/.codex/auth.json in Codex's exact shape so
    ``openseer.auth.load_tokens`` keeps working unchanged."""
    id_token = tokens.get("id_token", "") or ""
    access_token = tokens.get("access_token", "") or ""
    # `chatgpt_account_id` lives in the access_token's
    # `https://api.openai.com/auth` claim — that's where the rest of
    # openseer extracts it from. The id_token only carries standard
    # OpenID profile claims for some token grants, so falling back to
    # it would silently produce an empty account_id and the saved
    # auth.json would fail openseer.auth's validation. Read from the
    # access_token first, fall back to id_token only as a defensive
    # measure (codex P1).
    account_id = ""
    for tok in (access_token, id_token):
        payload = _decode_jwt_payload(tok)
        chatgpt_auth = payload.get("https://api.openai.com/auth", {}) or {}
        candidate = chatgpt_auth.get("chatgpt_account_id", "") or ""
        if candidate:
            account_id = candidate
            break
    if not account_id:
        # `openseer.openai_chatgpt._tokens()` rejects an auth file
        # without an account_id, so saving one would leave the user
        # in a state where login "succeeds" but tasks fail with
        # "missing access_token / account_id". Surface the actual
        # cause now (codex P2). Most likely cause: the requested
        # scopes/audience didn't produce the codex-backend access
        # token; double-check SCOPES against a fresh codex auth.json.
        raise RuntimeError(
            "OAuth tokens lacked `chatgpt_account_id` in their "
            "claims. The login server may not have granted the "
            "Codex-backend audience for these scopes. Refusing to "
            "overwrite ~/.codex/auth.json with an unusable file.")
    out = {
        "auth_mode": "chatgpt",
        # Codex CLI also populates OPENAI_API_KEY by exchanging the
        # access_token for an API key via a private endpoint we don't
        # mirror here. OpenSeer's runtime uses access_token directly,
        # so leaving this empty is fine.
        "OPENAI_API_KEY": "",
        "tokens": {
            "id_token":       id_token,
            "access_token":   access_token,
            "refresh_token":  tokens.get("refresh_token", "") or "",
            "account_id":     account_id,
        },
        "last_refresh": time.strftime(
            "%Y-%m-%dT%H:%M:%S.000000Z", time.gmtime()),
    }
    AUTH_FILE.parent.mkdir(parents=True, exist_ok=True)
    # Open with O_CREAT|O_WRONLY|O_TRUNC and explicit 0600 mode so
    # the file is private from the moment it exists. Using
    # `Path.write_text` then `os.chmod` would leave the file world-
    # readable for the duration of the write, briefly exposing the
    # token blob on multi-user systems with searchable home dirs
    # (codex P2). We still write to a tmp file + atomic rename so
    # a crash mid-flight can't leave a half-written auth.json.
    tmp = AUTH_FILE.with_suffix(".json.tmp")
    fd = os.open(str(tmp),
                 os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        # `mode` on os.open only applies when the file is CREATED.
        # If a stale tmp from a prior crash already exists with
        # a looser mode, open(...) reuses it. Force 0600 via fchmod
        # so we can never end up replacing auth.json with a
        # world-readable mode (codex P2).
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(out, fh, indent=2)
    except Exception:
        try: os.unlink(str(tmp))
        except OSError: pass
        raise
    os.replace(tmp, AUTH_FILE)


def run_login(timeout_s: int = 600, *, quiet: bool = False) -> int:
    """Browser-based ChatGPT OAuth login. Returns 0 on success.

    On macOS this opens the user's default browser to auth.openai.com,
    spins up a localhost callback server, captures the authorization
    code, exchanges it for tokens, and persists them to
    ``~/.codex/auth.json``.
    """
    chosen_port: int | None = None
    for p in [CALLBACK_PORT] + FALLBACK_PORTS:
        if _check_port_available(p):
            chosen_port = p
            break
    if chosen_port is None:
        ports = ", ".join(str(p) for p in [CALLBACK_PORT] + FALLBACK_PORTS)
        print(f"All registered callback ports ({ports}) are busy. "
              f"Close the other login flow and retry, or pass "
              f"`--legacy` to use the codex CLI flow instead.")
        return 1
    if chosen_port != CALLBACK_PORT and not quiet:
        print(f"Port {CALLBACK_PORT} busy — using fallback {chosen_port}.")
    # Auth0 validates redirect URI by exact string match. The Codex
    # OAuth client registers a small set of localhost callback ports;
    # we try each in order until one binds. Server below listens on
    # both 127.0.0.1 and ::1 so the browser's `localhost` lookup
    # reaches us regardless of which family resolves first.
    redirect_uri = f"http://localhost:{chosen_port}{CALLBACK_PATH}"
    verifier, challenge = _gen_pkce()
    state = _b64url(secrets.token_bytes(16))
    nonce = _b64url(secrets.token_bytes(16))
    auth_url = AUTHORIZE_URL + "?" + urllib.parse.urlencode({
        "response_type":         "code",
        "client_id":             CLIENT_ID,
        "redirect_uri":          redirect_uri,
        "scope":                 SCOPES,
        "code_challenge":        challenge,
        "code_challenge_method": "S256",
        "state":                 state,
        "nonce":                 nonce,
        # `prompt=login` would force re-auth even if browser cookies
        # are valid — we don't, so already-signed-in users complete
        # in one click.
    })

    captured: dict[str, Any] = {}
    handler = _build_callback_handler(state, captured)
    # Two loopback-only listeners (v4 + v6) — see
    # _start_loopback_listeners. Binding to ":: with V6ONLY=0" would
    # accept LAN traffic; we don't want that.
    v4, v6 = _start_loopback_listeners(chosen_port, handler)

    if not quiet:
        print("→ Opening browser to sign in with ChatGPT…")
        print(f"  If it doesn't open, paste this URL into a browser:\n"
              f"  {auth_url}")
    webbrowser.open(auth_url)

    deadline = time.monotonic() + timeout_s
    try:
        while time.monotonic() < deadline:
            if captured:
                break
            time.sleep(0.2)
    finally:
        for s in (v4, v6):
            s.shutdown()
            s.server_close()

    if "error" in captured:
        print(f"Login failed: {captured['error']}")
        return 1
    if "code" not in captured:
        print(f"Login timed out after {timeout_s}s with no callback.")
        return 1

    try:
        tokens = _exchange_code(captured["code"], verifier, redirect_uri)
    except Exception as e:
        print(f"Token exchange failed: {e!r}")
        return 1
    if not tokens.get("access_token"):
        print(f"Token endpoint returned no access_token: {tokens!r}")
        return 1

    try:
        _save_auth(tokens)
    except RuntimeError as e:
        # _save_auth raises if the tokens are missing the codex-
        # backend account_id claim — surface as a login failure
        # rather than crashing.
        print(f"Login failed: {e}")
        return 1
    if not quiet:
        print(f"✓ Signed in. Saved auth to {AUTH_FILE}")
    return 0
