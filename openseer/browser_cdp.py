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
    if len(sys.argv) < 3 or sys.argv[1] != "test":
        print("usage: python -m openseer.browser_cdp test <url>",
              file=sys.stderr)
        return 2
    url = sys.argv[2]
    try:
        title = _BackgroundLoop.shared().run(_smoke_test(url))
    except CDPError as e:
        print(f"FAIL: {e}", file=sys.stderr)
        return 1
    print(f"OK: title={title!r}")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
