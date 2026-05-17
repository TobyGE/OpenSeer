"""Chrome DevTools Protocol client for OpenSeer's browser actions.

Why this exists
---------------
OpenSeer's original browser path (`_read_page` in executor.py) drives
the user's frontmost browser via AppleScript `execute javascript`.
That works for a quick `innerText` grab but has hard limits:

  - synchronous-only evaluation: `await` / Promises return the literal
    string "[object Promise]" instead of the resolved value, so DOM-
    stable waiting, network capture, etc. are impossible
  - foreground-tab only: the agent has to steal user focus to operate
  - no element-by-selector targeting: click / type still go through
    pyautogui pixel coordinates
  - blocked by "Allow JavaScript from Apple Events" toggle being off

CDP fixes all four. We launch a *separate* Chrome instance with
`--remote-debugging-port` + a dedicated `user-data-dir`, talk to it
over the websocket DevTools exposes. The user's normal Chrome stays
untouched; users log into Twitter / Notion / etc. once inside the
OpenSeer profile and the cookies persist across runs.

Architecture (Day 1 scope — only `ChromeProcess` + `CDPClient` +
`Runtime.evaluate`):

    +---------------+              +-----------------------+
    | executor.py   |  --uses-->   | ChromeManager.shared  |
    | (sync)        |              | - launch / probe      |
    +---------------+              | - hand out CDPClient  |
                                    +----------|------------+
                                               |
                                    +----------v------------+
                                    | _BackgroundLoop       |
                                    | (asyncio in a thread) |
                                    +----------|------------+
                                               |  ws://127.0.0.1:9222
                                    +----------v------------+
                                    | OpenSeer Chrome       |
                                    | --user-data-dir=      |
                                    |   ~/.openseer/        |
                                    |   chrome-profile/     |
                                    +-----------------------+

Day 1 deliverable: `python -m openseer.browser_cdp test <url>`
launches Chrome (or reuses a live one), opens a tab, navigates,
returns `document.title`. Higher-level Tab methods (extract_text,
click, type, wait_dom_stable) land Day 2+.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import socket
import subprocess
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import websockets


log = logging.getLogger("openseer.browser_cdp")


# ── configuration ────────────────────────────────────────────────────

_PROFILE_DIR = Path.home() / ".openseer" / "chrome-profile"
# Stamp file written on every successful spawn. Stores the port + the
# spawned pid + a canonical profile path, so a subsequent run can
# verify "the Chrome answering on this port is ACTUALLY mine"
# before attaching. Without this validation a stale cache could
# point us at the user's regular Chrome (which is a disaster: we'd
# clobber their open tabs in the next smoke / read_page).
_STAMP_FILE = Path.home() / ".openseer" / "cdp-chrome.json"
_PORT_RANGE = range(9222, 9232)    # try 9222..9231 if earlier ones busy
_LAUNCH_TIMEOUT_S = 12
_RPC_TIMEOUT_S = 30

# Standard macOS install locations, ordered by user preference.
# We pick the first that exists; users who symlink Chrome elsewhere
# can override via OPENSEER_CHROME env var.
_CHROME_PATHS = [
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Google Chrome Canary.app/Contents/MacOS/Google Chrome Canary",
    "/Applications/Arc.app/Contents/MacOS/Arc",
    "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser",
    "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
]


# ── errors ───────────────────────────────────────────────────────────


class CDPError(Exception):
    """Raised by anything CDP-related so executor.py can catch one
    type and fall back to AppleScript without leaking implementation
    detail."""


# ── locating Chrome ──────────────────────────────────────────────────


def find_chrome_binary() -> str | None:
    """First Chromium-family binary that actually exists on disk.

    Override via `OPENSEER_CHROME` (full path to the Mach-O inside the
    .app, e.g. `/Applications/X.app/Contents/MacOS/X`). Returns None
    when nothing is installed; the caller is expected to surface that
    to the user instead of trying to launch.
    """
    override = os.environ.get("OPENSEER_CHROME")
    if override and Path(override).exists():
        return override
    for p in _CHROME_PATHS:
        if Path(p).exists():
            return p
    return None


# ── port discovery ───────────────────────────────────────────────────


def _is_port_free(port: int) -> bool:
    """True iff binding to 127.0.0.1:port would succeed RIGHT NOW.

    There's an unavoidable TOCTOU window between checking and launch,
    but Chrome's own port-grabbing handles the collision case cleanly
    by refusing to start; we re-probe in `wait_for_ready`.
    """
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            s.bind(("127.0.0.1", port))
            return True
    except OSError:
        return False


def _devtools_reachable(port: int, timeout: float = 0.4) -> bool:
    """True iff `http://127.0.0.1:<port>/json/version` answers. Quick
    way to detect a pre-existing OpenSeer Chrome we should attach to
    instead of spawning a new one."""
    try:
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/json/version")
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status == 200
    except (urllib.error.URLError, OSError, TimeoutError):
        return False


def _our_chrome_on_port(port: int) -> bool:
    """True iff a Chrome we previously spawned is still listening on
    `port`. We MUST verify the cmdline points at our profile dir —
    the user might be running their regular Chrome with
    `--remote-debugging-port=9222` for unrelated dev work, and
    attaching to that would clobber their tabs. Codex P2 on the
    Day 1 commit caught this.
    """
    if not _devtools_reachable(port):
        return False
    # `lsof` → pid of the LISTEN socket on that port.
    try:
        out = subprocess.run(
            ["lsof", "-iTCP:" + str(port), "-sTCP:LISTEN", "-t", "-n"],
            capture_output=True, text=True, timeout=1.5)
    except (OSError, subprocess.TimeoutExpired):
        return False
    pid_line = (out.stdout or "").strip().splitlines()
    if not pid_line:
        return False
    pid = pid_line[0]
    # Inspect that pid's command line. ps -ww disables column
    # truncation so a long --user-data-dir argument shows in full.
    try:
        ps = subprocess.run(
            ["ps", "-ww", "-p", pid, "-o", "command="],
            capture_output=True, text=True, timeout=1.5)
    except (OSError, subprocess.TimeoutExpired):
        return False
    cmd = (ps.stdout or "").strip()
    # Match against the absolute profile path we use. Both
    # `--user-data-dir=/abs/path` and `--user-data-dir /abs/path`
    # forms work; require the exact path so a sibling profile (eg.
    # an experimental OpenSeer fork on the same machine) doesn't
    # false-positive.
    needle = f"--user-data-dir={_PROFILE_DIR}"
    return needle in cmd


def _pick_port() -> int:
    """Reuse a live OpenSeer Chrome's port if one's verifiably ours.
    Otherwise scan for a free one.

    Stamp file (`~/.openseer/cdp-chrome.json`) carries port + pid +
    profile path; the live process at that port must still be one we
    spawned (matching cmdline) before we'll reattach. This is the
    isolation guarantee — `b1` only works if we never accidentally
    drive the user's normal Chrome.
    """
    if _STAMP_FILE.exists():
        try:
            stamp = json.loads(_STAMP_FILE.read_text())
            cached_port = int(stamp.get("port", 0))
        except (json.JSONDecodeError, OSError, ValueError):
            cached_port = 0
        if cached_port and _our_chrome_on_port(cached_port):
            return cached_port
    # Either no stamp, stamp pointed at a dead/foreign Chrome.
    # Scan upward for a fresh port that's free AND not already
    # answering DevTools (another Chromium running there is fine —
    # we just don't want to land on it).
    for p in _PORT_RANGE:
        if _is_port_free(p) and not _devtools_reachable(p):
            return p
    raise CDPError(
        f"no free port in {_PORT_RANGE.start}-{_PORT_RANGE.stop-1} for "
        "the OpenSeer Chrome debug interface")


def _write_stamp(port: int, pid: int | None) -> None:
    _STAMP_FILE.parent.mkdir(parents=True, exist_ok=True)
    _STAMP_FILE.write_text(json.dumps({
        "port": port,
        "pid": pid,
        "profile": str(_PROFILE_DIR),
    }))


# ── Chrome process lifecycle ─────────────────────────────────────────


@dataclass
class ChromeHandle:
    """Pointer to a launched (or reattached) OpenSeer Chrome."""
    port: int
    proc: subprocess.Popen | None   # None when we attached to a Chrome
                                     # we didn't start (eg. a daemon
                                     # restart finding an existing one)


def _build_launch_args(chrome_bin: str, port: int) -> list[str]:
    """Flags we hand the bundled Chrome.

    Each flag has a reason — keep them annotated so future-me doesn't
    "clean up" something load-bearing.
    """
    return [
        chrome_bin,
        f"--remote-debugging-port={port}",
        f"--user-data-dir={_PROFILE_DIR}",
        # Open with a blank tab — DON'T restore from the user's last
        # session and DON'T pop the "Welcome to Chrome" first-run UI.
        "about:blank",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-features=Translate,InfiniteSessionRestore",
        # We're driving a Chrome we own; suppress automated-Chrome
        # warning ribbon ("Chrome is being controlled by automated
        # test software") that some users find alarming on a regular
        # browsing window.
        "--disable-blink-features=AutomationControlled",
        # CRITICAL: drop the multiprocess-shared-memory dependency.
        # `/dev/shm` is tiny on macOS; without this, complex pages
        # OOM-tabs within seconds.
        "--disable-dev-shm-usage",
        # Reduce CPU on idle background tabs so we don't fight the
        # user's own Chrome for fan time.
        "--disable-background-timer-throttling=false",
    ]


def launch_chrome() -> ChromeHandle:
    """Spawn (or attach to) an OpenSeer-controlled Chrome.

    Returns a `ChromeHandle` whose `.port` answers DevTools requests.
    Raises CDPError if Chrome isn't installed, no port is available,
    or the spawned process doesn't open the DevTools endpoint within
    `_LAUNCH_TIMEOUT_S`. Callers should treat that as the cue to
    fall back to the AppleScript path silently.
    """
    chrome_bin = find_chrome_binary()
    if chrome_bin is None:
        raise CDPError(
            "no Chromium-family browser found in /Applications. "
            "Install Google Chrome (or set OPENSEER_CHROME) to enable "
            "the CDP-backed browser path.")
    port = _pick_port()
    # `_pick_port` returns a port that's either VERIFIED ours (live
    # Chrome whose cmdline matches our profile path) or empty/free.
    # In the verified-ours case `_our_chrome_on_port` already gated
    # the answer; attach with proc=None so future shutdown logic
    # knows "not our process, don't kill it."
    if _our_chrome_on_port(port):
        log.info("CDP attaching to existing OpenSeer Chrome on port %d",
                  port)
        _write_stamp(port, pid=None)
        return ChromeHandle(port=port, proc=None)

    _PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    args = _build_launch_args(chrome_bin, port)
    log.info("CDP launching Chrome on port %d (profile=%s)",
              port, _PROFILE_DIR)
    proc = subprocess.Popen(
        args,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        # Detach from controlling terminal so a user quitting OpenSeer
        # from a terminal doesn't SIGINT-cascade into Chrome.
        start_new_session=True,
    )
    _write_stamp(port, pid=proc.pid)

    # Poll the DevTools endpoint until it answers (Chrome's WS server
    # takes 1-3 seconds to come up cold).
    deadline = time.monotonic() + _LAUNCH_TIMEOUT_S
    while time.monotonic() < deadline:
        if _devtools_reachable(port):
            return ChromeHandle(port=port, proc=proc)
        if proc.poll() is not None:
            # Chrome exited before opening DevTools (port already in
            # use by a process we couldn't see, missing dyld lib, …).
            raise CDPError(
                f"Chrome exited with code {proc.returncode} before "
                "opening the DevTools endpoint")
        time.sleep(0.15)
    # Timed out — kill the child so we don't leak Chromes.
    try:
        proc.terminate()
    except OSError:
        pass
    raise CDPError(
        f"Chrome didn't open DevTools on :{port} within "
        f"{_LAUNCH_TIMEOUT_S}s")


def fetch_tabs(port: int) -> list[dict]:
    """List currently-open tabs (and other targets like background
    workers; we filter to type=page for navigation use)."""
    try:
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/json")
        with urllib.request.urlopen(req, timeout=2.0) as r:
            data = json.loads(r.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, TimeoutError,
            json.JSONDecodeError) as e:
        raise CDPError(f"couldn't list tabs on :{port}: {e}") from e
    return [t for t in data if t.get("type") == "page"]


# ── background asyncio loop ──────────────────────────────────────────


class _BackgroundLoop:
    """A single asyncio loop running on a dedicated daemon thread.

    Why this exists: `websockets` is async-native, but
    `openseer.executor` is sync (it's called from the agent's worker
    thread, no event loop). Building/tearing-down an event loop per
    CDP call would (a) be slow, (b) drop the WebSocket connection
    each time, defeating the point of persistent CDP sessions.

    Instead, we keep one loop forever, and synchronous callers
    submit coroutines via `run(coro)` which blocks the caller until
    the loop returns the result. This is the same pattern Playwright,
    Selenium, etc. use for sync APIs over async transports.
    """

    _instance: "_BackgroundLoop | None" = None
    _lock = threading.Lock()

    def __init__(self) -> None:
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(
            target=self._spin, name="openseer-cdp-loop", daemon=True)
        self.thread.start()

    def _spin(self) -> None:
        asyncio.set_event_loop(self.loop)
        self.loop.run_forever()

    def run(self, coro, *, timeout: float = _RPC_TIMEOUT_S) -> Any:
        """Submit a coroutine and wait for it. The timeout is a
        belt-and-suspenders guard against a hung CDP call locking the
        agent thread; CDPClient also has its own per-message
        deadline."""
        fut = asyncio.run_coroutine_threadsafe(coro, self.loop)
        return fut.result(timeout=timeout)

    @classmethod
    def shared(cls) -> "_BackgroundLoop":
        with cls._lock:
            if cls._instance is None:
                cls._instance = cls()
            return cls._instance


# ── CDP client (single WS connection) ────────────────────────────────


class CDPClient:
    """Wraps one websocket to a CDP target (a tab, in our usage).

    Each Chrome target exposes its own `webSocketDebuggerUrl` returned
    from `/json`; we open one client per target. Methods are async; the
    sync wrappers in CDPTab (Day 2) marshal through `_BackgroundLoop`.

    Concurrency: a single Chrome target only serves one debugger
    client at a time. If two OpenSeer instances try to attach to the
    same tab, the second one wins and the first one's socket closes.
    For now we assume single-daemon (the singleton guard in
    AgentdClient already enforces this).
    """

    def __init__(self, ws_url: str) -> None:
        self._ws_url = ws_url
        self._ws: Any = None        # websockets.WebSocketClientProtocol
        self._next_id = 0
        self._pending: dict[int, asyncio.Future] = {}
        self._recv_task: asyncio.Task | None = None

    async def connect(self) -> None:
        # Chrome rejects subprotocols and oversized frames in stdlib
        # default; bump max_size so a big DOM dump doesn't get
        # truncated mid-message.
        self._ws = await websockets.connect(
            self._ws_url, max_size=32 * 1024 * 1024, ping_interval=None)
        self._recv_task = asyncio.create_task(self._recv_loop())

    async def close(self) -> None:
        if self._recv_task is not None:
            self._recv_task.cancel()
        if self._ws is not None:
            await self._ws.close()
        # Reject any in-flight requests with a clear error so callers
        # blocked on `result()` don't hang forever.
        for fut in self._pending.values():
            if not fut.done():
                fut.set_exception(
                    CDPError("CDP connection closed mid-request"))
        self._pending.clear()

    async def _recv_loop(self) -> None:
        """Demux incoming JSON-RPC messages by id back to the futures
        their `send()` call is awaiting. Drops events (no `id` field)
        on the floor for Day 1 — Day 2 will route those to subscriber
        callbacks for things like Page.loadEventFired."""
        try:
            async for raw in self._ws:
                try:
                    msg = json.loads(raw)
                except json.JSONDecodeError:
                    log.warning("CDP non-json frame: %r", raw[:120])
                    continue
                mid = msg.get("id")
                if mid is None:
                    # Event, not a response. Day 1 ignores; Day 2
                    # will fan these out to subscribers.
                    continue
                fut = self._pending.pop(mid, None)
                if fut is None or fut.done():
                    continue
                if "error" in msg:
                    err = msg["error"]
                    fut.set_exception(CDPError(
                        f"{err.get('code')} {err.get('message','?')}"))
                else:
                    fut.set_result(msg.get("result", {}))
        except (websockets.ConnectionClosed, asyncio.CancelledError):
            return

    async def send(self, method: str, params: dict | None = None,
                   *, timeout: float = _RPC_TIMEOUT_S) -> dict:
        """Send one JSON-RPC request, await the response."""
        if self._ws is None:
            raise CDPError("CDP client not connected")
        mid = self._next_id
        self._next_id += 1
        fut: asyncio.Future = asyncio.get_event_loop().create_future()
        self._pending[mid] = fut
        payload = {"id": mid, "method": method}
        if params is not None:
            payload["params"] = params
        await self._ws.send(json.dumps(payload))
        try:
            return await asyncio.wait_for(fut, timeout=timeout)
        except asyncio.TimeoutError as e:
            self._pending.pop(mid, None)
            raise CDPError(f"CDP {method} timed out after {timeout}s") from e

    async def evaluate(self, expression: str,
                       await_promise: bool = True) -> Any:
        """Runtime.evaluate convenience — returns the resolved JS
        value (or raises with the JS exception message). `await_promise`
        is the whole reason we built this layer over AppleScript:
        finally we can `await` real promises and get the resolved
        value back."""
        await self.send("Runtime.enable")
        result = await self.send("Runtime.evaluate", {
            "expression": expression,
            "returnByValue": True,
            "awaitPromise": await_promise,
        })
        if "exceptionDetails" in result:
            ex = result["exceptionDetails"]
            txt = ((ex.get("exception") or {}).get("description")
                    or ex.get("text", "<unknown>"))
            raise CDPError(f"JS exception: {txt}")
        return (result.get("result") or {}).get("value")


# ── JS helpers ───────────────────────────────────────────────────────
# These are template strings with {placeholders} filled in by Python.
# Ported from OpenCLI's `dom-helpers.ts` waitForDomStableJs / clickJs /
# typeTextJs with three adaptations:
#   1. Single-string literal so we can str.format it from Python
#   2. JSON-encode all caller-supplied values to defend against
#      attribute / quote injection
#   3. No `data-opencli-ref` attribute lookup — we don't stamp refs;
#      we accept a raw CSS selector
#
# Why we don't import the strings from OpenCLI: it's TypeScript with
# its own build, and the file is small. Reimplementing in-line keeps
# the dep boundary clean and lets us evolve the JS without bumping
# an external version. Comments call out the OpenCLI lineage.

_DOM_STABLE_JS = """
new Promise(resolve => {{
  if (!document.body) {{
    setTimeout(() => resolve('nobody'), {max_ms});
    return;
  }}
  let timer = null;
  let cap = null;
  const obs = new MutationObserver(resetQuiet);
  function done(reason) {{
    clearTimeout(timer);
    clearTimeout(cap);
    obs.disconnect();
    resolve(reason);
  }}
  function resetQuiet() {{
    clearTimeout(timer);
    timer = setTimeout(() => done('quiet'), {quiet_ms});
  }}
  obs.observe(document.body, {{
    childList: true, subtree: true, attributes: true,
  }});
  resetQuiet();
  cap = setTimeout(() => done('capped'), {max_ms});
}})
"""

# Click via querySelector + el.click(). Works for ~95% of click
# targets; CDP-Input mouse-dispatch fallback can come later if we
# hit elements that need a real bubbled MouseEvent sequence.
_CLICK_JS = """
(() => {{
  const sel = {selector_json};
  let el = document.querySelector(sel);
  if (!el) {{
    // Try indexed access: selector === "12" → 12th matching tabbable.
    const idx = parseInt(sel, 10);
    if (!isNaN(idx)) {{
      el = document.querySelectorAll(
        'a, button, input, select, textarea, '
        + '[role="button"], [tabindex]:not([tabindex="-1"])')[idx];
    }}
  }}
  if (!el) throw new Error('Element not found: ' + sel);
  el.scrollIntoView({{ behavior: 'instant', block: 'center' }});
  const r = el.getBoundingClientRect();
  el.click();
  return {{
    status: 'clicked', x: Math.round(r.left + r.width/2),
    y: Math.round(r.top + r.height/2),
    w: Math.round(r.width), h: Math.round(r.height),
  }};
}})()
"""

# Type via the native setter so React/Vue / Lit-style controlled
# inputs fire change handlers correctly. For contenteditable we
# fall back to document.execCommand insertText — deprecated but
# the only path that fires the right composition + input event
# sequence for SPAs like the Twitter composer.
_TYPE_JS = """
(() => {{
  const sel = {selector_json};
  const text = {text_json};
  const el = document.querySelector(sel);
  if (!el) throw new Error('Element not found: ' + sel);
  el.focus();
  if (el.isContentEditable) {{
    const sel0 = window.getSelection();
    const range = document.createRange();
    range.selectNodeContents(el);
    sel0.removeAllRanges();
    sel0.addRange(range);
    document.execCommand('insertText', false, text);
    return {{ status: 'typed-contenteditable',
              length: text.length }};
  }}
  const proto = el.tagName === 'TEXTAREA'
    ? window.HTMLTextAreaElement.prototype
    : window.HTMLInputElement.prototype;
  const setter = Object.getOwnPropertyDescriptor(proto, 'value').set;
  setter.call(el, text);
  el.dispatchEvent(new Event('input', {{ bubbles: true }}));
  el.dispatchEvent(new Event('change', {{ bubbles: true }}));
  return {{ status: 'typed', length: text.length }};
}})()
"""


# ── CDPTab — page-level convenience wrapper ──────────────────────────


class CDPTab:
    """Wraps a single Chrome target (= page) with the methods the
    OpenSeer agent loop needs. All methods are async; the sync
    callers (executor.py via the bridge functions below) marshal
    through `_BackgroundLoop`.

    Lifecycle: open via `ChromeManager.open_tab()` or `current_tab()`,
    use, then `await close()`. Closing the tab here drops the CDP
    socket but does NOT close the Chrome target itself — the user
    can keep using it. Callers that want the tab gone too should
    `_close_target` separately.
    """

    def __init__(self, port: int, target: dict) -> None:
        self._port = port
        self._target = target
        self._client: CDPClient | None = None
        self._enabled = False

    async def _ensure_client(self) -> CDPClient:
        if self._client is None:
            ws = self._target.get("webSocketDebuggerUrl")
            if not ws:
                raise CDPError("target has no webSocketDebuggerUrl")
            self._client = CDPClient(ws)
            await self._client.connect()
        if not self._enabled:
            await self._client.send("Page.enable")
            await self._client.send("Runtime.enable")
            self._enabled = True
        return self._client

    async def close(self) -> None:
        if self._client is not None:
            await self._client.close()
            self._client = None

    @property
    def target_id(self) -> str:
        return str(self._target.get("id", ""))

    async def goto(self, url: str, *, wait_load: bool = True,
                   timeout: float = 20.0) -> None:
        """Navigate to `url`. When `wait_load` is true, ALSO waits
        for (a) the destination document to actually exist (the
        new `Runtime.evaluate` is hitting the new page, not the
        prior about:blank), and (b) the DOM to stabilize for ~400ms.

        Why the two-phase wait: `Page.navigate` returns as soon as
        Chrome has accepted the navigation request; it does NOT mean
        the new document has been parsed yet. Without phase (a) a
        fast follow-up `Runtime.evaluate` can hit the about:blank
        document, find a quiet DOM immediately, and `read_page_via_cdp`
        returns empty content — exactly the silent failure mode this
        method was built to avoid.

        Detection uses two independent signals: a URL change away
        from the pre-navigate href, OR a `readyState=='loading'`
        observation (proving the document went through a fresh load
        cycle). Either one combined with a final non-loading state
        is sufficient. Polling beats Page events here because our
        CDPClient demuxes by reply-id only — adding event dispatch
        would be more surface area than the problem warrants."""
        c = await self._ensure_client()
        try:
            pre_href = await self.evaluate(
                "location.href", await_promise=False)
        except CDPError:
            pre_href = None
        await c.send("Page.navigate", {"url": url})
        if wait_load:
            await self._wait_document_committed(
                pre_href=pre_href if isinstance(pre_href, str) else None,
                timeout=min(timeout, 8.0))
            try:
                await self.wait_dom_stable(
                    quiet_ms=400, max_ms=int(timeout * 1000))
            except CDPError:
                # Don't fail navigation just because DOM-stable timed
                # out; agents often want partial content anyway.
                pass

    async def _wait_document_committed(self, *,
                                       pre_href: str | None,
                                       timeout: float = 8.0) -> None:
        """Wait until we have evidence the destination document is
        live. Two independent qualifying signals (only one needed):

          - `location.href` differs from `pre_href` and isn't blank
            — Chrome has swapped to the new document
          - we observe `document.readyState=='loading'` at any point
            — proves the document went through a fresh load cycle
            (handles re-navigation to the same URL where href won't
            change)

        Either signal, combined with a final non-loading readyState
        (`interactive` or `complete`), means it's safe to query the
        page. If the timeout fires without that combination, return
        anyway — the goto() contract doesn't raise on slow pages."""
        deadline = time.monotonic() + timeout
        saw_loading = False
        while time.monotonic() < deadline:
            try:
                # Bundle href + readyState in one round-trip — keeps
                # the poll cycle cheap (~5-10ms per probe).
                probe = await self.evaluate(
                    "({h: location.href, s: document.readyState})",
                    await_promise=False)
            except CDPError:
                # Transient — Page.navigate can briefly leave Runtime
                # without an active execution context. Retry.
                await asyncio.sleep(0.1)
                continue
            href = probe.get("h") if isinstance(probe, dict) else None
            state = probe.get("s") if isinstance(probe, dict) else None
            url_changed = (isinstance(href, str)
                           and href != "about:blank"
                           and (pre_href is None or href != pre_href))
            if state == "loading":
                saw_loading = True
            if (url_changed or saw_loading) \
                    and state in ("interactive", "complete"):
                return
            await asyncio.sleep(0.05)

    async def wait_dom_stable(self, *, quiet_ms: int = 400,
                              max_ms: int = 4000) -> str:
        js = _DOM_STABLE_JS.format(quiet_ms=quiet_ms, max_ms=max_ms)
        return str(await self.evaluate(js, await_promise=True))

    async def evaluate(self, expression: str, *,
                       await_promise: bool = True) -> Any:
        c = await self._ensure_client()
        return await c.evaluate(expression, await_promise=await_promise)

    async def extract_text(self, *, selector: str | None = None,
                           max_chars: int = 8000) -> str:
        """`innerText` of selector match (or full body). Result is
        truncated to `max_chars` to match the existing AppleScript
        path's 8 kB cap."""
        if selector:
            js = (
                "(() => {"
                f"const el = document.querySelector({json.dumps(selector)});"
                "if (!el) return null;"
                f"return el.innerText.slice(0, {max_chars});"
                "})()"
            )
        else:
            js = (f"(document.body ? document.body.innerText : '')"
                   f".slice(0, {max_chars})")
        out = await self.evaluate(js, await_promise=False)
        return str(out or "")

    async def title(self) -> str:
        return str(await self.evaluate("document.title",
                                        await_promise=False))

    async def current_url(self) -> str:
        return str(await self.evaluate("location.href",
                                        await_promise=False))

    async def click(self, selector: str) -> dict:
        js = _CLICK_JS.format(selector_json=json.dumps(selector))
        out = await self.evaluate(js, await_promise=False)
        return out if isinstance(out, dict) else {"status": "ok"}

    async def type_text(self, selector: str, text: str) -> dict:
        js = _TYPE_JS.format(
            selector_json=json.dumps(selector),
            text_json=json.dumps(text))
        out = await self.evaluate(js, await_promise=False)
        return out if isinstance(out, dict) else {"status": "ok"}


def _close_target(port: int, target_id: str) -> None:
    """HTTP request to close a tab. Best-effort; ignored on error."""
    try:
        urllib.request.urlopen(
            f"http://127.0.0.1:{port}/json/close/{target_id}",
            timeout=2.0).read()
    except (urllib.error.URLError, OSError, TimeoutError):
        pass


# ── ChromeManager — process-wide singleton ───────────────────────────


class ChromeManager:
    """Single OpenSeer Chrome per Python process.

    The agent loop runs synchronously in a worker thread; ChromeManager
    is the bridge it talks to for "I need a browser tab now." We keep
    the launched ChromeHandle cached so the second read_page in a run
    doesn't pay the 1-3s Chrome cold-start tax.
    """

    _instance: "ChromeManager | None" = None
    _lock = threading.Lock()

    @classmethod
    def shared(cls) -> "ChromeManager":
        with cls._lock:
            if cls._instance is None:
                cls._instance = cls()
            return cls._instance

    def __init__(self) -> None:
        self._handle: ChromeHandle | None = None
        self._handle_lock = threading.Lock()
        # target_id of the most recent CDP read. Used to pin
        # follow-up selector clicks/types to the same tab even when
        # `/json` MRU ordering changes (browser pop-up activates a
        # different tab, user clicks into the OpenSeer Chrome, etc.).
        self._last_target_id: str | None = None
        self._last_target_lock = threading.Lock()

    def remember_target(self, target_id: str) -> None:
        with self._last_target_lock:
            self._last_target_id = target_id

    def _remembered_target(self) -> str | None:
        with self._last_target_lock:
            return self._last_target_id

    def ensure_running(self) -> ChromeHandle:
        """Returns a usable ChromeHandle, launching if needed. Idempotent.
        Re-launches if a previously-cached handle's port no longer
        answers DevTools (Chrome was quit by the user, OS killed it,
        etc.) OR if the port is now held by some OTHER Chrome (we got
        race-displaced after our Chrome exited and a regular Chrome
        bound the port). The lsof+cmdline cross-check is the SAME
        guard `launch_chrome` uses on first attach — without it here
        we'd silently route the agent into the user's regular Chrome
        and clobber whatever they had open. Real edge case but the
        blast radius (writing into the user's tabs) is large enough
        that paying ~5ms of lsof per request is fine."""
        with self._handle_lock:
            if self._handle is not None:
                port = self._handle.port
                if _devtools_reachable(port) and _our_chrome_on_port(port):
                    return self._handle
                log.info(
                    "CDP cached Chrome on :%d no longer ours (gone or "
                    "displaced); relaunching", port)
                self._handle = None
            self._handle = launch_chrome()
            return self._handle

    def open_tab(self, url: str | None = None) -> CDPTab:
        """Create a NEW target pointing at `url` (or about:blank if
        not provided) and return a CDPTab bound to it. The caller
        owns the tab — close it via `tab.close()` then optionally
        `_close_target(port, tab.target_id)`."""
        handle = self.ensure_running()
        target_url = url or "about:blank"
        target = _new_tab(handle.port, target_url)
        return CDPTab(handle.port, target)

    def front_tab(self) -> CDPTab | None:
        """Return a CDPTab to operate on. Preference order:

          1. The target_id remembered by the last successful CDP
             read (via `remember_target`), if it still exists. This
             is what binds `click(selector=...)` to the tab that
             `read_page(url=...)` just populated — `/json` MRU order
             is not stable across multiple reads (a popup, an
             extension, or the user clicking into OpenSeer Chrome
             can promote a different tab to front), and selector
             actions silently hitting the wrong tab would corrupt
             the read_page → click workflow.
          2. Fall back to MRU (`/json[0]`) when no remembered target
             exists or that target was closed — best effort for
             ad-hoc one-off click(selector) calls.

        Returns None if there are no page targets at all."""
        handle = self.ensure_running()
        tabs = fetch_tabs(handle.port)
        if not tabs:
            return None
        remembered = self._remembered_target()
        if remembered:
            for t in tabs:
                if t.get("id") == remembered:
                    return CDPTab(handle.port, t)
            # Remembered target was closed — clear the cache so we
            # don't keep racing back to it.
            with self._last_target_lock:
                if self._last_target_id == remembered:
                    self._last_target_id = None
        return CDPTab(handle.port, tabs[0])

    def shutdown(self) -> None:
        """Kill the Chrome we spawned (if any). Doesn't touch
        Chromes we merely attached to. Mostly for tests."""
        with self._handle_lock:
            h = self._handle
            self._handle = None
        if h is None or h.proc is None:
            return
        try:
            h.proc.terminate()
        except OSError:
            pass


# ── sync bridges for executor.py ─────────────────────────────────────


def cdp_available() -> bool:
    """Cheap, non-throwing probe — does the system have what we need?
    Used by executor.py as the gate: try CDP only if this returns
    True, otherwise fall straight through to AppleScript without
    paying the launch cost or the user-visible Chrome flash.
    """
    # Hard gate via env var so a user can force-off if CDP is misbehaving.
    mode = (os.environ.get("OPENSEER_BROWSER_CDP", "auto") or "").lower()
    if mode == "off":
        return False
    return find_chrome_binary() is not None


def cdp_required() -> bool:
    """True when the user has set `OPENSEER_BROWSER_CDP=on` — in
    that mode a CDP failure should surface to the model as an error
    instead of silently falling back to AppleScript. Useful for
    diagnosing why CDP isn't being picked up."""
    return (os.environ.get("OPENSEER_BROWSER_CDP", "auto") or "")\
        .lower() == "on"


# ── Article extraction (Mozilla Readability + html2text) ─────────────
#
# `read_page` was historically `document.body.innerText` truncated to
# 8 kB — a giant string that dragged in nav, sidebar, footer, cookie
# banner, related-content sidebar, ad slots, and the actual article
# all mashed together. The model spent token budget reading the
# cruft and lost structure (no headings, lists, tables — innerText
# flattens everything).
#
# Better path:
#   1. After the page has rendered (handled by goto / _wait_document_committed),
#      inject Mozilla Readability (Apache-2.0; vendored under
#      openseer/browser_assets/readability/) into the page.
#   2. Readability picks the dominant article subtree and returns
#      clean HTML — nav/footer/ads stripped out.
#   3. We pull that HTML back to Python and run html2text on it to
#      produce compact Markdown that preserves headings, lists,
#      code blocks, tables, and link text.
#
# Three modes the executor (or any caller) can pick:
#   - "article": run Readability; raise if no article is found
#   - "raw":     skip Readability entirely, return body.innerText (today's behaviour)
#   - "auto":    try Readability, fall back to raw innerText on miss.
#                Default — best UX, never empty unless the page truly is.


_READABILITY_DIR = Path(__file__).resolve().parent / "browser_assets" / "readability"
_readability_cache: tuple[str, str] | None = None


def _readability_sources() -> tuple[str, str]:
    """Read Readability.js + Readability-readerable.js from disk
    (lazily, once per process). Returns (readability_src, readerable_src)."""
    global _readability_cache
    if _readability_cache is not None:
        return _readability_cache
    try:
        readability = (_READABILITY_DIR / "Readability.js").read_text(
            encoding="utf-8")
        readerable = (_READABILITY_DIR / "Readability-readerable.js").read_text(
            encoding="utf-8")
    except FileNotFoundError as e:
        raise CDPError(
            f"Readability source missing at {_READABILITY_DIR}. The wheel "
            f"may have been built without browser_assets package-data. "
            f"Reinstall the package or copy Readability.js + "
            f"Readability-readerable.js from upstream "
            f"(github.com/mozilla/readability).") from e
    _readability_cache = (readability, readerable)
    return _readability_cache


# Heuristic fallback chain when Readability declines to parse.
# Matches OpenCLI's pick for the same problem (article-extract.ts).
_DEFAULT_FALLBACK_SELECTORS = [
    "main", '[role="main"]', "#main-content", "#main",
    "#content", ".content", "article", "body",
]
_FALLBACK_MIN_TEXT = 80  # chars; below this a fallback root is skipped


def _build_extract_article_js(*, force: bool = False,
                               clean_selectors: list[str] | None = None,
                               fallback_selectors: list[str] | None = None,
                               ) -> str:
    """Build the JS expression that runs in-page. Adapted from
    OpenCLI's article-extract.ts buildExtractArticleJs — same
    high-level pipeline, slimmed down where we don't need every
    feature.

    Returns the JS string. Inject via tab.evaluate(..., await_promise=False)
    since the body is sync (no awaits inside).
    """
    readability, readerable = _readability_sources()
    return f"""
(() => {{
  const cleanSelectors = {json.dumps(clean_selectors or [])};
  const fallbackSelectors = {json.dumps(fallback_selectors
                                          or _DEFAULT_FALLBACK_SELECTORS)};
  const force = {json.dumps(bool(force))};
  const minFallbackText = {_FALLBACK_MIN_TEXT};
  const readabilitySrc = {json.dumps(readability)};
  const readerableSrc = {json.dumps(readerable)};

  // Cap every outgoing payload at 200 kB BEFORE serializing back
  // through Runtime.evaluate. Two reasons:
  //   1. websocket frame limits (we set max_size=32 MB, but the
  //      JSON round-trip + ProtocolReply size is what burns memory)
  //   2. html2text is fast but not free; feeding it the full DOM
  //      of a 10 MB SPA wastes seconds. Python-side max_chars caps
  //      the FINAL markdown; this caps the raw HTML on the way out.
  const MAX_HTML_BYTES = 200000;
  function cap(html) {{
    if (typeof html !== "string") return {{ html: "", truncated: false }};
    if (html.length <= MAX_HTML_BYTES) {{
      return {{ html: html, truncated: false }};
    }}
    return {{ html: html.slice(0, MAX_HTML_BYTES), truncated: true }};
  }}

  function esc(s) {{
    return String(s).replace(/[&<>]/g,
      c => ({{ "&": "&amp;", "<": "&lt;", ">": "&gt;" }}[c]));
  }}

  // Short-circuit: non-HTML document. The browser may render a raw
  // text / JSON file inside <pre>; pass it through as-is rather than
  // sending Readability after something it can't make sense of.
  const ct = document.contentType || "";
  if (ct && ct !== "text/html" && ct !== "application/xhtml+xml") {{
    const body = document.body ? (document.body.textContent || "") : "";
    // Cap the inner text BEFORE the <pre> wrap so the whole payload
    // stays under MAX_HTML_BYTES even when esc() expands a few chars.
    const slice = body.length > MAX_HTML_BYTES
      ? body.slice(0, MAX_HTML_BYTES) : body;
    return {{ source: "raw-text",
             html: "<pre>" + esc(slice) + "</pre>",
             title: document.title || "",
             truncated: body.length > MAX_HTML_BYTES }};
  }}
  if (document.body) {{
    const kids = document.body.children;
    if (kids.length === 1 && kids[0] && kids[0].tagName === "PRE") {{
      const capped = cap(document.body.outerHTML);
      return {{ source: "pre",
               html: capped.html,
               title: document.title || "",
               truncated: capped.truncated }};
    }}
  }}

  // Deep-clone the doc; mutations on the clone don't affect the
  // live page (we may still want to click / type later).
  const cloneDoc = document.cloneNode(true);
  for (const sel of cleanSelectors) {{
    try {{ for (const n of cloneDoc.querySelectorAll(sel)) n.remove(); }}
    catch (e) {{ /* invalid selector — ignore */ }}
  }}

  // Inject Readability sources inside an isolated Function scope
  // so their var declarations don't pollute window.* on the live
  // page. Both library files include a CommonJS guard that's falsy
  // here, so the constructor lands in local scope and we return it.
  const libs = (new Function(
    readabilitySrc + "\\n" + readerableSrc + "\\nreturn {{" +
    " Readability: typeof Readability !== 'undefined' ? Readability : null," +
    " isProbablyReaderable: typeof isProbablyReaderable !== 'undefined' ? isProbablyReaderable : null" +
    " }};"
  ))();
  const Readability = libs.Readability;
  const isProbablyReaderable = libs.isProbablyReaderable;

  const readerableOk = force ||
    (typeof isProbablyReaderable === "function"
      ? isProbablyReaderable(cloneDoc) : true);
  let article = null;
  if (readerableOk && typeof Readability === "function") {{
    try {{ article = new Readability(cloneDoc).parse(); }}
    catch (e) {{ article = null; }}
  }}
  if (article && article.content) {{
    const capped = cap(article.content);
    return {{
      source: "readability",
      html: capped.html,
      title: article.title || document.title || "",
      byline: article.byline || null,
      publishedTime: article.publishedTime || null,
      siteName: article.siteName || null,
      excerpt: article.excerpt || null,
      truncated: capped.truncated,
    }};
  }}

  // Fallback chain: pick the first big-enough structural container.
  // Uses the same `cap()` as the other paths — a `body` match on a
  // heavy SPA can be megabytes of hydration scripts.
  for (const sel of fallbackSelectors) {{
    let el = null;
    try {{ el = cloneDoc.querySelector(sel); }} catch (e) {{ continue; }}
    if (!el) continue;
    const text = (el.textContent || "").trim();
    if (text.length < minFallbackText) continue;
    const capped = cap(el.outerHTML || "");
    return {{ source: "fallback", html: capped.html,
             title: document.title || "",
             truncated: capped.truncated }};
  }}
  return null;
}})()
"""


def _html_to_markdown(html: str) -> str:
    """HTML → Markdown via html2text. Tuned for LLM consumption:
    no body wrap (long lines are fine, no need to break for humans),
    no ASCII rulers (`* * *`) for <hr>, preserve link text.

    html2text is imported lazily so import-time isn't slowed by it
    on hosts that never use the article path."""
    import html2text
    h = html2text.HTML2Text()
    h.body_width = 0          # don't wrap — flat lines tokenize better
    h.ignore_images = True    # alt text wins; img src is noise
    h.ignore_emphasis = False
    h.ignore_links = False
    h.protect_links = True    # never break a URL across lines
    h.single_line_break = True
    h.escape_snob = True      # preserve angle brackets etc. literally
    md = h.handle(html)
    # Strip trailing whitespace on every line — html2text leaves
    # trailing spaces after each paragraph and they bloat tokens.
    return "\n".join(line.rstrip() for line in md.splitlines()).strip()


async def _extract_article_inpage(tab: "CDPTab") -> dict | None:
    """Run Readability inside the tab. Returns a dict shaped like
    OpenCLI's ExtractedArticle (source, html, title, byline, …)
    or None when both Readability + the fallback chain fail."""
    js = _build_extract_article_js()
    out = await tab.evaluate(js, await_promise=False)
    if not isinstance(out, dict):
        return None
    if "html" not in out or "source" not in out:
        return None
    return out


def _run_cdp(coro_factory, *, what: str) -> Any:
    """Submit a coroutine-producing callable to the background loop
    and uniformly convert any leaked exception into CDPError. The
    executor's fallback layer only catches CDPError, so a stray
    OSError from a half-open socket, `websockets.ConnectionClosed`,
    or the background-loop's `concurrent.futures.TimeoutError`
    would otherwise escape and abort the agent step instead of
    falling through to AppleScript. Centralizing the wrap here
    keeps the contract honest at every bridge entry point."""
    try:
        return _BackgroundLoop.shared().run(coro_factory())
    except CDPError:
        raise
    except Exception as e:
        raise CDPError(f"{what}: {type(e).__name__}: {e}") from e


def read_page_via_cdp(*, url: str | None = None,
                      selector: str | None = None,
                      mode: str = "auto",
                      max_chars: int = 8000) -> dict:
    """Sync entry point used by executor._read_page. Returns a dict
    ``{title, url, content, source}`` where ``source`` reports which
    extraction path produced ``content``:

      - ``"readability"`` — Mozilla Readability picked an article
      - ``"fallback"``    — Readability declined; we returned the
                            first big-enough structural container
      - ``"pre"`` / ``"raw-text"`` — non-HTML doc passed through
      - ``"selector"``    — caller passed ``selector=...``, we
                            returned that element's innerText
      - ``"innerText"``   — neither Readability nor selector were
                            used; full ``body.innerText`` dump

    ``mode`` picks the strategy:
      - ``"auto"`` (default) — try Readability; fall back to
        body.innerText if no article. Best UX: cleaner data when
        we have it, never empty when Readability misses.
      - ``"article"`` — only try Readability. Raises CDPError if
        nothing usable comes back (caller asked for an article,
        and there isn't one).
      - ``"raw"``     — skip Readability; behave like the old
        path (full body.innerText, truncated).

    ``selector`` (when set) overrides ``mode`` — caller knows
    exactly which element they want.

    Raises CDPError on transport / launch failure; the executor
    decides whether to fall back to the AppleScript path.
    """
    if mode not in ("auto", "article", "raw"):
        raise ValueError(
            f"read_page_via_cdp mode must be auto|article|raw, got {mode!r}")

    async def _run() -> dict:
        mgr = ChromeManager.shared()
        if url:
            # `/json/new?url=...` (inside open_tab) ALREADY initiated
            # navigation to `url`. Calling Page.navigate(url) here
            # would be a redundant second nav AND would defeat the
            # `_wait_document_committed` heuristic: pre_href would
            # already equal url, the readyState may have raced past
            # 'loading', and we'd burn the full 8s timeout on every
            # call. Just wait for the already-in-flight load.
            tab = mgr.open_tab(url)
            # Pin THIS tab as the follow-up selector target so a
            # later click/type doesn't race onto whichever tab `/json`
            # happens to report first.
            if tab.target_id:
                mgr.remember_target(tab.target_id)
        else:
            tab = mgr.front_tab()
            if tab is None:
                tab = mgr.open_tab("about:blank")
        try:
            if url:
                await tab._ensure_client()
                await tab._wait_document_committed(
                    pre_href=None, timeout=8.0)
                try:
                    await tab.wait_dom_stable(
                        quiet_ms=400, max_ms=8000)
                except CDPError:
                    pass
            else:
                await tab.wait_dom_stable(quiet_ms=300, max_ms=2500)
            title = await tab.title()
            current = await tab.current_url()

            # ── Selector path takes precedence ──────────────────────
            # The caller said "I want THIS element," so honor it
            # regardless of mode. Mostly used for "give me the
            # comments section only" or "extract this code block."
            if selector:
                content = await tab.extract_text(
                    selector=selector, max_chars=max_chars)
                if not content:
                    content = "(selector matched no element)"
                return {"title": title, "url": current,
                        "content": content, "source": "selector"}

            # ── raw mode: full innerText, current behavior ─────────
            if mode == "raw":
                content = await tab.extract_text(max_chars=max_chars)
                return {"title": title, "url": current,
                        "content": content, "source": "innerText"}

            # ── auto / article: try Readability ────────────────────
            article = await _extract_article_inpage(tab)
            # In strict `article` mode, only a real Readability hit
            # counts. The fallback chain (`<main>`, `<body>`, …) is
            # essentially what `raw` mode already gives, just rendered
            # through html2text — accepting it would make "article"
            # indistinguishable from "auto" on most pages with a
            # non-empty body. Force-fallthrough to the strict-error
            # branch below by zeroing out non-readability hits.
            if (mode == "article" and article
                    and article.get("source") != "readability"):
                article = None
            if article and article.get("html"):
                md = _html_to_markdown(article["html"])
                # Two failure modes the fallback path is prone to:
                #   1. The selected <body> matched mostly because of
                #      script/style/hydration-data textContent, so
                #      html2text strips it all and we're left with
                #      ~nothing visible.
                #   2. We capped the HTML in-page and the resulting
                #      Markdown happens to fit under max_chars, so
                #      our Python-side truncation marker never fires
                #      — the agent gets a silent partial.
                # For (1): when the fallback path's Markdown is empty
                # or near-empty, fall through to innerText below
                # (which preserves visible text including in deeper
                # body subtrees). For (2): respect the JS-side
                # `truncated` flag and surface the marker.
                stripped = md.strip()
                if (article.get("source") == "fallback"
                        and len(stripped) < 40):
                    article = None
                else:
                    if len(md) > max_chars:
                        md = md[:max_chars].rstrip() + "\n\n[truncated]"
                    elif article.get("truncated"):
                        md = md.rstrip() + "\n\n[truncated (HTML cap)]"
                    head_bits = []
                    if article.get("byline"):
                        head_bits.append(f"_By {article['byline']}_")
                    if article.get("publishedTime"):
                        head_bits.append(f"_{article['publishedTime']}_")
                    if article.get("siteName"):
                        head_bits.append(f"_via {article['siteName']}_")
                    meta = " · ".join(head_bits)
                    article_title = article.get("title") or title
                    content = (f"{meta}\n\n{md}" if meta else md)
                    return {"title": article_title or title,
                            "url": current,
                            "content": content,
                            "source": article.get("source") or "readability"}

            # ── auto fallback / article failure ────────────────────
            if mode == "article":
                raise CDPError(
                    f"Readability could not extract an article from "
                    f"{current!r}. Set mode='auto' to fall back to "
                    f"innerText, or mode='raw' to skip extraction.")
            content = await tab.extract_text(max_chars=max_chars)
            return {"title": title, "url": current,
                    "content": content, "source": "innerText"}
        finally:
            # Drop the websocket but LEAVE the OS-level tab open: the
            # agent's natural follow-up is `click(selector=...)` /
            # `type(selector=...)` against the same page, and those
            # bind to ChromeManager.front_tab(). Closing the target
            # here would force the model into a "read_page → empty
            # browser → reopen URL → click" loop that defeats the
            # whole point of the URL+selector workflow. Tabs
            # accumulate over a run, but the OpenSeer Chrome is per-
            # process and short-lived; we'll revisit lifecycle when
            # multi-tab workflows show up.
            await tab.close()
    return _run_cdp(_run, what="read_page")


def click_via_cdp(selector: str) -> dict:
    """Click an element by CSS selector in the front-most OpenSeer
    Chrome tab. Used by executor when an Action has `selector` set."""
    async def _run() -> dict:
        tab = ChromeManager.shared().front_tab()
        if tab is None:
            raise CDPError(
                "no open tab to click into — use read_page with a url "
                "first, or open_app the browser to give it a page")
        try:
            return await tab.click(selector)
        finally:
            await tab.close()
    return _run_cdp(_run, what="click")


def type_via_cdp(selector: str, text: str) -> dict:
    """Type `text` into the element matching `selector` on the
    front-most OpenSeer Chrome tab."""
    async def _run() -> dict:
        tab = ChromeManager.shared().front_tab()
        if tab is None:
            raise CDPError(
                "no open tab to type into — open the page first")
        try:
            return await tab.type_text(selector, text)
        finally:
            await tab.close()
    return _run_cdp(_run, what="type")


# ── Day 1 smoke test ─────────────────────────────────────────────────


def _new_tab(port: int, url: str) -> dict:
    """Ask Chrome to open a FRESH tab pointed at `url`. We
    intentionally avoid reusing `tabs[0]` (which would clobber
    whatever the user has open in the OpenSeer profile — codex P2
    on the Day 1 commit). The PUT shape `/json/new?url=...` returns
    the new target's metadata including its webSocketDebuggerUrl.
    """
    from urllib.parse import quote
    try:
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/json/new?{quote(url, safe=':/?&=')}",
            method="PUT")
        with urllib.request.urlopen(req, timeout=4.0) as r:
            return json.loads(r.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, TimeoutError,
            json.JSONDecodeError) as e:
        raise CDPError(f"couldn't open a new tab on :{port}: {e}") from e


async def _smoke_test(url: str) -> str:
    """End-to-end Day 1 check: launch (or attach to) Chrome, open a
    NEW tab pointed at `url`, return `document.title`, then close
    that tab so the smoke test leaves no trace. Run via:

        python -m openseer.browser_cdp test https://example.com
    """
    handle = launch_chrome()
    target = _new_tab(handle.port, url)
    target_id = target.get("id")
    ws_url = target.get("webSocketDebuggerUrl")
    if not ws_url:
        raise CDPError("/json/new didn't return webSocketDebuggerUrl")
    client = CDPClient(ws_url)
    await client.connect()
    try:
        await client.send("Page.enable")
        # Page.navigate + a generous fixed wait for Day 1 (real DOM-
        # stable / load-event wait lands Day 2). The /json/new URL
        # parameter already triggers navigation, so just settle.
        await asyncio.sleep(2.5)
        title = await client.evaluate("document.title")
        return str(title)
    finally:
        await client.close()
        # Close the tab we created so the smoke test is idempotent.
        if target_id:
            try:
                urllib.request.urlopen(
                    f"http://127.0.0.1:{handle.port}/json/close/{target_id}",
                    timeout=2.0).read()
            except (urllib.error.URLError, OSError, TimeoutError):
                pass


def _main() -> int:
    import sys
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(message)s",
    )
    if len(sys.argv) < 3 or sys.argv[1] not in ("test", "read"):
        print("usage:\n"
              "  python -m openseer.browser_cdp test <url>\n"
              "  python -m openseer.browser_cdp read <url>",
              file=sys.stderr)
        return 2
    cmd, url = sys.argv[1], sys.argv[2]
    try:
        if cmd == "test":
            title = _BackgroundLoop.shared().run(_smoke_test(url))
            print(f"OK: title={title!r}")
        else:
            out = read_page_via_cdp(url=url, max_chars=200)
            print(f"OK: title={out['title']!r} "
                  f"url={out['url']!r} "
                  f"content[:200]={out['content']!r}")
    except CDPError as e:
        print(f"FAIL: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
