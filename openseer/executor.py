"""Action execution via pyautogui.

Logical coordinates assumed (matches ``screen.capture()`` output).
Out-of-bounds coordinates are clamped + warned. ``dry_run=True`` prints
actions without executing — used by default until the user opts in with
``--execute``.
"""
from __future__ import annotations

import shlex
import subprocess
import time
from dataclasses import dataclass

import pyautogui

# Don't fail-safe on tiny mouse moves to corner — agents may legitimately go there
pyautogui.FAILSAFE = True
pyautogui.PAUSE = 0.1


@dataclass
class Action:
    name: str                      # click | type | key | scroll | wait | open_app | bash | web_search | web_fetch | reground | terminate
    x: int | None = None
    y: int | None = None
    text: str | None = None
    key: str | None = None
    amount: int | None = None
    count: int = 1                 # for click: number of clicks (1=single, 2=double, ...)
    app: str | None = None         # for open_app
    cmd: str | None = None         # for bash
    cwd: str | None = None         # for bash (working directory; default: current)
    timeout: int = 30              # for bash (seconds)
    query: str | None = None       # for web_search
    url: str | None = None         # for web_fetch
    freshness: str | None = None   # for web_search: "day" | "week" | "month" | "year"
    skill_name: str | None = None  # for read_skill / write_skill: skill identifier
    skill_body: str | None = None  # for write_skill: full SKILL.md contents
    index: int | None = None       # for click: AX element index (preferred over x/y)
    attachments: list[str] | None = None  # for terminate: file paths to send back to user
    target: str | None = None      # natural-language element description; resolved to (x,y) by Grounder
    region: list[int] | None = None  # for reground: [x1, y1, x2, y2] crop bbox to "zoom" before grounding
    external: bool = False           # for reground: True ⇒ call the specialist (paid) grounder, not the default
    selector: str | None = None    # for read_page: optional CSS selector to extract a specific element instead of full body
    status: str | None = None      # for terminate: "done" | "fail"
    reason: str | None = None
    thought: str | None = None
    verified_by_steps: list[int] | None = None   # for terminate(done): prior step indices that ACTUALLY produced this result
    grounding_backend: str | None = None         # which grounder produced (x,y) when from target
    grounding_elapsed_ms: int | None = None


def _clamp(x: int, lo: int, hi: int) -> int:
    return max(lo, min(hi - 1, x))


def _paste_unicode_preserving_clipboard(text: str) -> None:
    """Type non-ASCII text via cmd+V while saving and restoring the user's
    full clipboard contents (text, image, files, rich text, AND multi-item
    selections like several files copied from Finder). Requires AppKit
    (PyObjC); raises RuntimeError if unavailable so we never silently
    clobber the user's clipboard."""
    try:
        from AppKit import NSPasteboard, NSPasteboardItem  # type: ignore[import-untyped]
    except Exception as e:
        raise RuntimeError(
            "non-ASCII typing requires AppKit / PyObjC. Install via "
            "`pip install pyobjc-framework-Cocoa`."
        ) from e
    pb = NSPasteboard.generalPasteboard()
    # Snapshot every item, copying each (type, data) into a fresh
    # NSPasteboardItem so the snapshot survives `clearContents`.
    saved: list = []
    for item in (pb.pasteboardItems() or []):
        copy = NSPasteboardItem.alloc().init()
        for t in (item.types() or []):
            data = item.dataForType_(t)
            if data is not None:
                copy.setData_forType_(data, t)
        saved.append(copy)
    try:
        pb.clearContents()
        pb.setString_forType_(text, "public.utf8-plain-text")
        time.sleep(0.05)
        pyautogui.hotkey("command", "v")
        time.sleep(0.10)
    finally:
        # Restore. If the pasteboard was empty before, leave it empty
        # rather than leaking the agent's text into it.
        pb.clearContents()
        if saved:
            pb.writeObjects_(saved)


# Browsers we know how to talk to via AppleScript.
# Chromium-family apps share Chrome's `execute ... javascript` syntax.
# Includes pre-release channels (Beta/Canary/Dev/Nightly) — these are
# distinct apps with their own AppleScript-resolvable names; treating
# them as the stable channel would silently drive the wrong browser.
_CHROMIUM_BROWSERS = {
    "Google Chrome", "Google Chrome Canary", "Google Chrome Beta",
    "Google Chrome Dev",
    "Microsoft Edge", "Microsoft Edge Beta", "Microsoft Edge Canary",
    "Microsoft Edge Dev",
    "Brave Browser", "Brave Browser Beta", "Brave Browser Nightly",
    "Arc", "Vivaldi", "Opera",
}


_BROWSER_ALIASES = (
    # (substring lower-cased, canonical AppleScript app name)
    # ORDER MATTERS — longer / more specific variants must come first
    # so e.g. "Microsoft Edge Beta" maps to Edge Beta, not stable Edge.
    # First match wins (substring search; we return immediately).
    ("google chrome canary",   "Google Chrome Canary"),
    ("chrome canary",          "Google Chrome Canary"),
    ("google chrome beta",     "Google Chrome Beta"),
    ("chrome beta",            "Google Chrome Beta"),
    ("google chrome dev",      "Google Chrome Dev"),
    ("chrome dev",             "Google Chrome Dev"),
    ("google chrome",          "Google Chrome"),
    ("chrome",                 "Google Chrome"),
    ("microsoft edge canary",  "Microsoft Edge Canary"),
    ("edge canary",            "Microsoft Edge Canary"),
    ("microsoft edge beta",    "Microsoft Edge Beta"),
    ("edge beta",              "Microsoft Edge Beta"),
    ("microsoft edge dev",     "Microsoft Edge Dev"),
    ("edge dev",               "Microsoft Edge Dev"),
    ("microsoft edge",         "Microsoft Edge"),
    ("edge",                   "Microsoft Edge"),
    ("brave browser nightly",  "Brave Browser Nightly"),
    ("brave nightly",          "Brave Browser Nightly"),
    ("brave browser beta",     "Brave Browser Beta"),
    ("brave beta",             "Brave Browser Beta"),
    ("brave browser",          "Brave Browser"),
    ("brave",                  "Brave Browser"),
    ("safari",                 "Safari"),
    ("arc",                    "Arc"),
    ("vivaldi",                "Vivaldi"),
    ("opera",                  "Opera"),
    ("firefox",                "Firefox"),
)


def _canonicalize_browser(name: str) -> str:
    """Map common aliases ("Chrome", "Edge", "Brave") to the exact app
    names AppleScript's `tell application "..."` expects ("Google
    Chrome", "Microsoft Edge", "Brave Browser"). Returns ``name``
    unchanged if no alias matches — that lets unknown values flow
    through to the executor's existing error path.
    """
    if not name:
        return name
    if name in _CHROMIUM_BROWSERS or name == "Safari":
        return name
    lower = name.strip().lower()
    for substr, canonical in _BROWSER_ALIASES:
        if substr in lower:
            return canonical
    return name


def _detect_frontmost_browser() -> str | None:
    """Return the localized name of the frontmost browser app, or None.

    Uses Quartz-based live frontmost detection (via ax.active_app_pid)
    rather than NSWorkspace.frontmostApplication(): NSWorkspace caches
    its answer and only updates when CFRunLoop is pumped, which our
    CLI/daemon Python process never does. Reading the cached value
    would mis-detect Safari/Arc/Edge as the host terminal in daemon
    mode and silently fall back to Chrome — operating the wrong
    browser is worse than asking the model to pass `app` explicitly.
    """
    try:
        from .ax import active_app_pid
        from AppKit import NSRunningApplication  # type: ignore[import-untyped]
        pid = active_app_pid()
        if not pid:
            return None
        ra = NSRunningApplication.runningApplicationWithProcessIdentifier_(pid)
        name = str(ra.localizedName() or "") if ra is not None else ""
    except Exception:
        return None
    canonical = _canonicalize_browser(name)
    # Only return when the canonicalized result is actually a known
    # browser — otherwise (e.g. frontmost is Notes / Mail / Slack)
    # we want None so the caller falls back to the default.
    if canonical == "Safari" or canonical in _CHROMIUM_BROWSERS \
            or canonical == "Firefox":
        return canonical
    return None


def _browser_current_url(app: str) -> str | None:
    """Probe the active tab's URL via JavaScript ``location.href``.

    Why JS over AppleScript ``URL of active tab``: Chrome's
    AppleScript URL property lags reality by 1-3 seconds during
    navigation (verified in trace 08ebeeac — `cmd+L`+type+enter
    fired at step3, but the AS URL property kept returning the
    PRIOR page's URL through step 5). Running JS via "execute
    javascript" / "do JavaScript" reads ``location.href`` in the
    actual page context, so it reflects the current page as soon
    as Chrome has navigated.

    Requires the user to enable "Allow JavaScript from Apple
    Events" in their browser (same gate as full read_page). Falls
    back to AppleScript URL property if JS is gated.

    Returns None on any failure.
    """
    js = "location.href"
    js_esc = js.replace("\\", "\\\\").replace('"', '\\"')
    app_esc = app.replace("\\", "\\\\").replace('"', '\\"')
    if app == "Safari":
        script = (f'tell application "{app_esc}" to '
                  f'do JavaScript "{js_esc}" in front document')
        as_fallback = f'tell application "{app_esc}" to URL of front document'
    elif app in _CHROMIUM_BROWSERS or "chrome" in app.lower():
        script = (f'tell application "{app_esc}" to execute front window\'s '
                  f'active tab javascript "{js_esc}"')
        as_fallback = (f'tell application "{app_esc}" to URL of active tab '
                       f'of front window')
    else:
        return None
    try:
        r = subprocess.run(["osascript", "-e", script],
                           capture_output=True, text=True, timeout=3)
    except Exception:
        r = None
    if r is None or r.returncode != 0:
        # JS gated or page mid-transition — use the AppleScript
        # property as a less-accurate fallback rather than failing.
        try:
            r = subprocess.run(["osascript", "-e", as_fallback],
                               capture_output=True, text=True, timeout=3)
        except Exception:
            return None
        if r.returncode != 0:
            return None
    out = (r.stdout or "").strip()
    return out or None


def _browser_state_probe(app: str) -> dict | None:
    """Lightweight load-stability probe. Single JS call returns both
    ``document.title`` and ``document.body.innerText.length`` so the
    poll loop has TWO signals: title change confirms the SPA has
    actually navigated to a different page (DOM swapped, not just
    location.href), and length tells us whether the new content has
    finished rendering. ~30-50 ms per call.

    Returns None on any failure (JS gated, app gone, page in transition).
    """
    js = ('JSON.stringify({title:document.title,'
          'len:(document.body && document.body.innerText.length) || 0})')
    js_esc = js.replace("\\", "\\\\").replace('"', '\\"')
    app_esc = app.replace("\\", "\\\\").replace('"', '\\"')
    if app == "Safari":
        script = (f'tell application "{app_esc}" to '
                  f'do JavaScript "{js_esc}" in front document')
    elif app in _CHROMIUM_BROWSERS or "chrome" in app.lower():
        script = (f'tell application "{app_esc}" to execute front window\'s '
                  f'active tab javascript "{js_esc}"')
    else:
        return None
    try:
        r = subprocess.run(["osascript", "-e", script],
                           capture_output=True, text=True, timeout=3)
    except Exception:
        return None
    if r.returncode != 0:
        return None
    out = (r.stdout or "").strip()
    try:
        import json as _json
        d = _json.loads(out)
        return {"title": str(d.get("title") or ""),
                "len": int(d.get("len") or 0)}
    except Exception:
        return None


def _browser_innertext_length(app: str) -> int | None:
    """Backwards-compat wrapper around _browser_state_probe; returns
    just the length component."""
    s = _browser_state_probe(app)
    return s["len"] if s else None


def read_page_auto(app: str, *,
                   expect_change: bool = False,
                   previous_title: str | None = None,
                   settle_timeout: float = 3.5,
                   substantial_threshold: int = 6000,
                   stable_threshold: int = 2000,
                   growth_tolerance: float = 0.05,
                   poll_interval: float = 0.8) -> str | None:
    """Auto-perception variant. Polls innerText.length until the page
    looks loaded, then does a full read_page fetch. Returns the
    page-text block, or None on any failure (silent: the agent loop
    has screenshot + AX as fallback).

    ``expect_change=True`` is set by the agent loop when the URL just
    changed. SPAs (X / LinkedIn / Reddit / etc) use ``pushState``:
    ``location.href`` flips synchronously, but the DOM's ``innerText``
    keeps showing the OLD content for 1–3 s while the SPA fetches
    and re-renders. With ``expect_change=False`` we'd hit the
    fast-path on the very first sample (stale content >= 6000 chars
    looks "loaded"), capture the old page under the new URL, and
    confuse the model. With ``expect_change=True`` we skip the
    fast-path and require a stability streak of at least two samples
    so the new content has time to replace the old.

    Stability rules (any one stops the poll):
      - (only when expect_change=False) first sample is already
        ≥ substantial_threshold chars → page is clearly loaded
      - latest sample is ≥ stable_threshold AND grew less than
        growth_tolerance over the previous sample (need ≥ 2 samples
        when expect_change=True so we observe the SPA replacement)
      - settle_timeout elapsed → give up waiting; fetch what's there
      - probe failed (JS gated, page error) → return None

    Defaults: 3.5s timeout for steady pages, bumped to 5s when
    expect_change=True to cover slower SPA renders.
    """
    cap = 5.0 if expect_change else settle_timeout
    deadline = time.monotonic() + cap
    last_len = -1
    samples = 0
    # `document.title` is the right signal for "did the SPA actually
    # navigate to a new page?". location.href flips synchronously on
    # pushState (so URL change alone says nothing about DOM swap), but
    # most SPAs update the title only after the new page's data is
    # ready. innerText length is unreliable: similar pages (e.g. two
    # X searches) can have nearly identical lengths, and our cached
    # text is truncated, so a length comparison would fire false
    # positives on long pages. When previous_title is provided and
    # the current title still equals it, we know the swap hasn't
    # happened yet — keep waiting. When it differs, treat that as
    # confirmation and let the stability/fast-path checks proceed.
    saw_change = (not expect_change) or (previous_title in (None, ""))
    while time.monotonic() < deadline:
        s = _browser_state_probe(app)
        if s is None:
            return None                           # JS gated / app gone
        cur_title, cur_len = s["title"], s["len"]
        samples += 1
        if not saw_change and cur_title and cur_title != previous_title:
            saw_change = True
        # Fast path: first sample is already substantial AND we know
        # this is the new page (saw_change True via title diff, or we
        # weren't expecting a change at all). Otherwise we have to
        # wait for the SPA to swap.
        if (samples == 1 and cur_len >= substantial_threshold and saw_change):
            break
        if samples >= 2 and cur_len >= stable_threshold and last_len > 0:
            growth = abs(cur_len - last_len) / max(last_len, 1)
            if growth < growth_tolerance and saw_change:
                break
        last_len = cur_len
        # Don't sleep past the deadline; just exit and use what we have.
        if time.monotonic() + poll_interval >= deadline:
            break
        time.sleep(poll_interval)

    a = Action(name="read_page", app=app)
    try:
        result = _read_page(a, dry_run=False)
    except Exception:
        return None
    if not result or result.startswith("ERROR") or result.startswith("("):
        return None
    return result


def _read_page(action: "Action", *, dry_run: bool) -> str:
    """Extract page text from the active tab of a browser via AppleScript
    JavaScript injection. Lets the agent consume a webpage's content in
    one turn instead of scroll-and-screenshot loops.

    Optional fields on the Action:
      - ``url``: navigate the active tab to this URL first
      - ``app``: target a specific browser (default: detect frontmost
        browser, fall back to "Google Chrome")
      - ``selector``: a CSS selector — if given, extract that element's
        innerText instead of the full body

    Requires the user to have enabled "Allow JavaScript from Apple
    Events" once in the browser (Safari: Develop menu → Allow JS from
    Apple Events; Chrome: View → Developer → Allow JavaScript from
    Apple Events; Arc: same path as Chrome). Without that, AppleScript
    refuses to run JS and we surface a clear error telling the user
    where to enable it.
    """
    import json
    app_arg = (action.app or "").strip()
    app = _canonicalize_browser(app_arg) if app_arg \
        else (_detect_frontmost_browser() or "Google Chrome")
    url = (action.url or "").strip()
    selector = (action.selector or "").strip()

    if dry_run:
        return f"would read_page in {app!r}" + (f" after navigate {url}" if url else "")

    # AppleScript string literal escaping: backslash + double-quote.
    # Without this, a URL or app name containing `"` or `\` breaks
    # the script — or worse, lets malicious input inject AppleScript
    # commands. Same escape we use in open_app for app names.
    def _as_esc(s: str) -> str:
        return s.replace("\\", "\\\\").replace('"', '\\"')
    app_esc = _as_esc(app)

    # Step 1: optional navigation
    if url:
        url_esc = _as_esc(url)
        if app == "Safari":
            nav = f'tell application "{app_esc}" to set URL of front document to "{url_esc}"'
        elif app in _CHROMIUM_BROWSERS or "chrome" in app.lower():
            nav = (f'tell application "{app_esc}" to set URL of active tab '
                   f'of front window to "{url_esc}"')
        else:
            return (f"ERROR: read_page can't navigate {app!r}. Open the URL "
                    f"first via `bash open '{url}'`, then call read_page.")
        try:
            r = subprocess.run(["osascript", "-e", nav],
                               capture_output=True, text=True, timeout=8)
        except subprocess.TimeoutExpired:
            return f"ERROR: navigation to {url} timed out in {app}"
        if r.returncode != 0:
            err = (r.stderr or "").strip()
            if "isn't running" in err.lower() or "not running" in err.lower():
                return (f"ERROR: {app} isn't running. Use open_app or "
                        f"`bash open -a \"{app}\"` first.")
            return f"ERROR: navigation failed in {app}: {err[:300]}"
        time.sleep(2.5)               # let the page load before we read

    # Step 2: build JS to extract content
    if selector:
        js = ("(function(){"
              f"var el=document.querySelector({json.dumps(selector)});"
              "var txt=el?el.innerText:'(selector matched no element)';"
              "return JSON.stringify({title:document.title,url:location.href,"
              "content:txt.slice(0,8000)});})()")
    else:
        js = ("JSON.stringify({title:document.title,url:location.href,"
              "content:(document.body?document.body.innerText:'').slice(0,8000)})")

    js_for_as = _as_esc(js)
    if app == "Safari":
        applescript = (f'tell application "{app_esc}" to do JavaScript '
                       f'"{js_for_as}" in front document')
    elif app in _CHROMIUM_BROWSERS or "chrome" in app.lower():
        applescript = (f'tell application "{app_esc}" to execute front window\'s '
                       f'active tab javascript "{js_for_as}"')
    elif "firefox" in app.lower():
        return ("ERROR: Firefox doesn't support AppleScript JS injection. "
                "Use a Chromium browser (Chrome/Arc/Edge/Brave) or Safari, "
                "or fall back to `bash + curl` for static content.")
    else:
        return f"ERROR: read_page doesn't know how to talk to {app!r}."

    try:
        r = subprocess.run(["osascript", "-e", applescript],
                           capture_output=True, text=True, timeout=15)
    except subprocess.TimeoutExpired:
        return f"ERROR: {app} JS execution timed out (page may be stuck loading)"

    if r.returncode != 0:
        err = (r.stderr or "").strip()
        # Surface the most common gotcha — the "Allow JavaScript from
        # Apple Events" toggle isn't on by default — with explicit
        # one-shot instructions so the model can tell the user.
        low = err.lower()
        if ("doesn't understand" in low and "execute" in low) \
                or "javascript" in low and "allow" in low \
                or "1743" in err:
            menu = ("View → Developer → 'Allow JavaScript from Apple Events'"
                    if app != "Safari"
                    else "Develop → 'Allow JavaScript from Apple Events' "
                         "(enable Develop menu first in Safari Settings → "
                         "Advanced)")
            return (f"ERROR: {app} refused JS injection. Enable it once via "
                    f"the app's menu: {menu}. Then retry.")
        if "isn't running" in low or "not running" in low:
            return (f"ERROR: {app} isn't running. Use open_app or "
                    f"`bash open -a \"{app}\"` first.")
        return f"ERROR: AppleScript failed in {app}: {err[:300]}"

    raw = (r.stdout or "").strip()
    if not raw:
        return f"(no content returned from {app})"
    try:
        data = json.loads(raw)
        title = (data.get("title") or "").strip()
        page_url = (data.get("url") or "").strip()
        content = (data.get("content") or "").strip()
        head = f"# {title}" if title else "# (untitled)"
        return f"{head}\n{page_url}\n\n{content}"
    except Exception:
        # JSON parse failed — return raw text. Could happen if the page's
        # JSON.stringify hit non-UTF-8 or the AppleScript truncated.
        return raw[:8500]


def execute(action: Action, *, dry_run: bool = True) -> str:
    """Execute one action. Returns a short result/error string for the next-turn prompt."""
    w, h = pyautogui.size()
    name = action.name

    # ─── control flow ─────────────────────────────────────────────────────
    if name == "terminate":
        st = (action.status or "").lower() or "done"
        return f"task ended: {st} — {action.reason or ''}"
    # backward-compat alias for old `done`/`fail`
    if name in ("done", "fail"):
        return f"task ended: {name} — {action.reason or ''}"

    if name == "wait":
        s = max(0.1, min(5, action.amount or 1))
        if not dry_run:
            time.sleep(s)
        return f"waited {s}s"

    # ─── computer-use primitives ──────────────────────────────────────────
    if name in ("click", "scroll"):
        # If `index` is given on a click and no x,y, the agent loop has
        # already resolved (x,y) from the AX tree (see _resolve_index in
        # agent.py); here we only need to validate that we have coords.
        if action.x is None or action.y is None:
            if action.index is not None:
                return (f"ERROR: click(index={action.index}) but the AX-tree "
                        "lookup did not produce coordinates. The agent loop "
                        "resolves index→(x,y) — this means either the index "
                        "is out of range, or AX returned no elements this "
                        "turn (Catalyst app, no permission, or focus issue).")
            return "ERROR: missing x,y"
        x = _clamp(int(action.x), 0, w)
        y = _clamp(int(action.y), 0, h)
        clamped = (x, y) != (int(action.x), int(action.y))
        if name == "click":
            count = max(1, int(action.count or 1))
            if not dry_run:
                pyautogui.click(x, y, clicks=count, interval=0.06 if count > 1 else 0)
            verb = "clicked" if count == 1 else f"{count}-clicked"
            return f"{verb} ({x},{y})" + (" [clamped]" if clamped else "")
        if name == "scroll":
            amt = int(action.amount or 0)
            if not dry_run:
                pyautogui.moveTo(x, y)
                pyautogui.scroll(-amt)  # pyautogui: negative=down on macOS
            return f"scrolled at ({x},{y}) by {amt}"
    # backward-compat alias
    if name == "double_click":
        if action.x is None or action.y is None:
            return "ERROR: missing x,y"
        x = _clamp(int(action.x), 0, w)
        y = _clamp(int(action.y), 0, h)
        if not dry_run:
            pyautogui.doubleClick(x, y)
        return f"double-clicked ({x},{y})"

    if name == "open_app":
        app = (action.app or action.text or "").strip()
        if not app:
            return "ERROR: open_app needs `app` (the application name)"
        if not dry_run:
            r = subprocess.run(["open", "-a", app],
                               capture_output=True, text=True, timeout=8)
            if r.returncode != 0:
                stderr = (r.stderr or "").strip()
                return f"open -a {app!r} failed (rc={r.returncode}): {stderr[:200]}"
            # `open -a` brings the app's window forward in z-order but
            # does NOT steal keyboard focus from whatever's currently
            # frontmost (typically OpenSeer's own terminal). Without a
            # real focus shift, NSWorkspace.frontmostApplication keeps
            # returning the terminal and our AX dump runs against the
            # wrong app. Force focus via AppleScript `activate` — it
            # works on macOS 14+ where NSRunningApplication's
            # programmatic activate is silently ignored.
            try:
                time.sleep(0.4)               # let the app finish launching
                # AppleScript needs DOUBLE-quoted string literals with
                # backslash escapes for `\` and `"`. shlex.quote uses
                # shell-style SINGLE quotes which AppleScript parses as
                # an identifier (so multi-word names like "Google Chrome"
                # silently fail).
                _esc = app.replace("\\", "\\\\").replace('"', '\\"')
                subprocess.run(
                    ["osascript", "-e",
                     f'tell application "{_esc}" to activate'],
                    capture_output=True, timeout=4,
                )
            except Exception:
                # AppKit not available or activation refused — agent
                # loop's settle delay still gives the user a chance to
                # see the new window even if focus didn't transfer.
                pass
        return f"opened app {app!r}"

    if name == "type":
        if not action.text:
            return "ERROR: missing text"
        # If x,y given, click target first to establish focus. This is the
        # robust pattern (matches Anthropic/OpenAI computer-use APIs) — the
        # model declares "type X into the field at (x,y)" and the executor
        # owns the click-then-type sequencing, so the model can't forget
        # focus or have its click+type interleaved across turns.
        clicked_first = False
        if action.x is not None and action.y is not None:
            x = _clamp(int(action.x), 0, w)
            y = _clamp(int(action.y), 0, h)
            if not dry_run:
                pyautogui.click(x, y)
                time.sleep(0.15)  # let focus settle
            clicked_first = True
        # pyautogui.typewrite only handles keys in its US-keyboard map and
        # silently drops anything else (CJK, emoji, accented chars). For
        # non-ASCII text, route through the clipboard + cmd+V instead so
        # the field actually receives the user's payload.
        text = action.text
        non_ascii = any(ord(ch) > 127 for ch in text)
        method = "typewrite"
        if not dry_run:
            if non_ascii:
                method = "paste"
                try:
                    _paste_unicode_preserving_clipboard(text)
                except Exception as e:
                    # Don't crash the whole run — return the failure so the
                    # model can switch strategy (e.g. type ASCII, or paste
                    # via bash pbcopy + key cmd+v manually).
                    return (f"ERROR: paste-typing non-ASCII failed: {e}. "
                            f"Click step (if any) already happened. Try a "
                            f"different approach.")
            else:
                pyautogui.typewrite(text, interval=0.02)
        prefix = f"clicked ({action.x},{action.y}) → " if clicked_first else ""
        return f"{prefix}typed[{method}] {text!r}"

    if name == "key":
        combo = action.key or ""
        if not combo:
            return "ERROR: missing key"
        # accept "cmd+space", "ctrl+a", "enter", "tab", ...
        keys = [k.strip().lower() for k in combo.split("+")]
        # normalise mac modifiers + common keyname spellings (pyautogui uses
        # `pageup`/`pagedown` without separators; models often emit
        # `page_up`/`page-up`/`pgup` etc.)
        norm = {
            "cmd": "command", "opt": "option", "ctrl": "ctrl",
            "page_up": "pageup", "page-up": "pageup", "pgup": "pageup",
            "page_down": "pagedown", "page-down": "pagedown", "pgdn": "pagedown",
            "esc": "escape",
        }
        keys = [norm.get(k, k) for k in keys]
        if not dry_run:
            if len(keys) == 1:
                pyautogui.press(keys[0])
            else:
                pyautogui.hotkey(*keys)
        return f"pressed {'+'.join(keys)}"

    # ─── web ──────────────────────────────────────────────────────────────
    if name == "web_search":
        from .web import web_search
        q = (action.query or action.text or "").strip()
        if not q:
            return "ERROR: web_search needs `query`"
        if dry_run:
            return f"would search: {q!r}"
        return web_search(q, count=int(action.amount or 5),
                          freshness=action.freshness)

    if name == "read_page":
        return _read_page(action, dry_run=dry_run)

    if name == "web_fetch":
        from .web import web_fetch
        u = (action.url or action.text or "").strip()
        if not u:
            return "ERROR: web_fetch needs `url`"
        if dry_run:
            return f"would fetch: {u}"
        return web_fetch(u)

    # ─── shell ────────────────────────────────────────────────────────────
    if name == "bash":
        cmd = (action.cmd or "").strip()
        if not cmd:
            return "ERROR: bash needs `cmd`"
        timeout = max(1, min(120, int(action.timeout or 30)))
        cwd = action.cwd or None
        if dry_run:
            return f"would run: {cmd[:200]}"
        try:
            r = subprocess.run(
                cmd, shell=True, capture_output=True, text=True,
                timeout=timeout, cwd=cwd,
            )
        except subprocess.TimeoutExpired:
            return f"bash TIMEOUT after {timeout}s: {cmd[:120]}"
        except OSError as e:
            return f"bash exec error: {e}"
        # Return a compact result for the next-turn prompt. Truncate aggressively.
        out = (r.stdout or "")[:1500]
        err = (r.stderr or "")[:500]
        head = f"rc={r.returncode}"
        parts = [head]
        if out.strip():
            parts.append(f"stdout:\n{out}")
        if err.strip():
            parts.append(f"stderr:\n{err}")
        if not out.strip() and not err.strip():
            parts.append("(no output)")
        return "\n".join(parts)

    return f"ERROR: unknown action {name}"
