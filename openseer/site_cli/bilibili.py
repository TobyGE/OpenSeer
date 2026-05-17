"""Bilibili site CLI — `openseer site bili {search|hot|video}`.

Ported from OpenCLI/clis/bilibili/ (https://github.com/JackWener/OpenCLI,
Apache 2.0). Bilibili's web API requires WBI signing for several
endpoints (search, user info, …); the signing key rotates with the
visitor's session. OpenCLI's trick is to do the signing INSIDE the
authenticated browser context — `page.evaluate("fetch('/x/.../nav',
{credentials:'include'})")` gets the current keys, the JS-side
computes the signature, then a second `page.evaluate(fetch(signed_url))`
makes the real call. That keeps cookies / WBI in one place.

We mirror exactly that: every API call goes through CDPTab.evaluate
in our OpenSeer-owned Chrome (browser_cdp.py), so the user's logged-in
session in the OpenSeer Chrome profile is the auth source.

If you haven't logged into bilibili in OpenSeer Chrome yet, search /
hot still work (those endpoints don't require login), but anything
needing user state (favorites, dynamic feed) will fail with an auth
error. Log in once via `openseer site bili open` … TODO when we add
an "open this URL in the OpenSeer chrome" helper.
"""
from __future__ import annotations

import argparse
import json
import re
from typing import Any

from .. import browser_cdp
from .base import SiteCommand


_API_BASE = "https://api.bilibili.com"

# WBI mixin table (from OpenCLI; verified against the public WBI
# signing reference — this exact 64-element permutation is the
# bilibili web client's published mapping). Used to derive
# `mixin_key = first 32 chars of (img_key + sub_key permuted via
# this table)`. Without it, signed endpoints (search, user info)
# return -403 / -412 / empty payloads.
_MIXIN_TABLE = [
    46, 47, 18,  2, 53,  8, 23, 32, 15, 50, 10, 31, 58,  3, 45, 35,
    27, 43,  5, 49, 33,  9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13,
    37, 48,  7, 16, 24, 55, 40, 61, 26, 17,  0,  1, 60, 51, 30,  4,
    22, 25, 54, 21, 56, 59,  6, 63, 57, 62, 11, 36, 20, 34, 44, 52,
]


# ─── HTML strip + JS helpers ─────────────────────────────────────────


def _strip_html(s: str) -> str:
    s = re.sub(r"<[^>]+>", "", s)
    s = re.sub(r"&[a-z]+;", " ", s, flags=re.IGNORECASE)
    return s.strip()


def _ensure_bili_tab() -> browser_cdp.CDPTab:
    """Make sure a tab is open at bilibili.com (so cookies + WBI
    keys come from a same-origin context). Reuses an existing bili
    tab when possible — burns the launch tax only on first call.

    Resilience: front_tab() may return a stuck tab from a previous
    failed goto; current_url() against it could block. Wrap the
    probe in a short per-call timeout so we fail fast and fall
    through to opening a fresh tab instead of hanging the worker.
    """
    mgr = browser_cdp.ChromeManager.shared()
    tab = mgr.front_tab()
    # Probe with a 3s budget — if the existing tab is healthy this
    # is instant; if it's stuck, we want to move on quickly, not
    # block on the default 30s RPC timeout.
    #
    # Reuse criteria: tab is on bilibili.com **home or search**, not
    # on a video / live / dynamic page. Heavy sub-pages can hang JS
    # evaluate for many seconds even when current_url() returns
    # quickly; a tab we'd then try to fetch from would burn the
    # 30s RPC timeout. Always landing back on the home page is
    # cheap (cached) and gives us a known-stable evaluation context.
    if tab is not None:
        url = ""
        try:
            url = browser_cdp._BackgroundLoop.shared().run(
                tab.current_url(), timeout=3.0) or ""
        except Exception:
            pass
        # Acceptable: a bilibili.com URL that's either the bare
        # home, OR a search page. Crucially the host check guards
        # the `/search` branch — without it any google.com/search
        # / github.com/search etc. would be accepted and the
        # subsequent same-origin fetch to api.bilibili.com would
        # hit a CORS rejection.
        is_bili_host = bool(re.match(
            r"^https?://(?:www\.)?bilibili\.com(?:[/?#]|$)", url))
        is_bili_home = bool(re.match(
            r"^https?://(?:www\.)?bilibili\.com/?(?:\?|#|$)", url))
        ok = is_bili_home or (is_bili_host and "/search" in url)
        if ok:
            return tab
        # Tab isn't reusable, but it may belong to a parallel
        # agent step / prior unrelated workflow (a half-open
        # Notion page, a research thread, the OAuth callback
        # window, …). DO NOT close it — just drop our local
        # websocket and open a fresh bilibili tab alongside.
        # Closing what we didn't open would be silent state loss
        # the user can't recover from.
        try:
            browser_cdp._BackgroundLoop.shared().run(
                tab.close(), timeout=2.0)  # closes WS only, not OS tab
        except Exception:
            pass
    new_tab = mgr.open_tab("https://www.bilibili.com")
    if new_tab.target_id:
        mgr.remember_target(new_tab.target_id)

    # Wait for nav to commit so the cookie jar is populated, but
    # cap aggressively — 4s for the document, 2s for DOM stable.
    # Bilibili's home page is heavy and never reaches "quiet"; we
    # only need API state, not a fully rendered page.
    async def _wait():
        await new_tab._ensure_client()
        await new_tab._wait_document_committed(pre_href=None, timeout=4.0)
        try:
            await new_tab.wait_dom_stable(quiet_ms=300, max_ms=2000)
        except browser_cdp.CDPError:
            pass
    try:
        browser_cdp._BackgroundLoop.shared().run(_wait(), timeout=8.0)
    except Exception:
        # If even the bounded wait fails, return the tab anyway —
        # the subsequent fetch will surface a clearer error than a
        # bare TimeoutError.
        pass
    return new_tab


def _eval(tab: browser_cdp.CDPTab, js: str) -> Any:
    """Run JS inside the bili tab, return the resolved value.

    Routed through _run_cdp so a leaked websockets ConnectionClosed
    / OSError / asyncio.TimeoutError from a stale tab becomes a
    CDPError — which the cli dispatcher catches. Without the wrap
    the bilibili command would print a raw traceback whenever the
    cached tab's websocket had died (e.g. after sleep/wake)."""
    return browser_cdp._run_cdp(
        lambda: tab.evaluate(js, await_promise=True),
        what="bilibili eval")


def _bili_get_nav(tab: browser_cdp.CDPTab) -> dict:
    """Hit /x/web-interface/nav inside the page context. Returns
    the parsed JSON; raises CDPError on transport failure."""
    js = """
    (async () => {
      const r = await fetch(
        'https://api.bilibili.com/x/web-interface/nav',
        { credentials: 'include' });
      return await r.json();
    })()
    """
    return _eval(tab, js) or {}


def _wbi_sign(tab: browser_cdp.CDPTab, params: dict) -> dict:
    """Compute w_rid + wts for `params` using current WBI keys.

    The full signing dance:
      1. GET /x/web-interface/nav → wbi_img.{img_url, sub_url}
      2. img_key/sub_key are the basenames sans extension
      3. mixin_key = first 32 chars of (img_key+sub_key) permuted
         via _MIXIN_TABLE
      4. wts = current unix seconds
      5. Sort params by key, drop !'()* from each value, build query
         string with %20 (NOT +) for spaces — bilibili rejects + as
         a signature mismatch
      6. w_rid = md5(query + mixin_key)

    Done JS-side inside the page so the request is same-origin and
    Network panel sees a normal fetch, not a cross-process call.
    """
    nav = _bili_get_nav(tab)
    img_url = (((nav.get("data") or {}).get("wbi_img") or {})
                .get("img_url") or "")
    sub_url = (((nav.get("data") or {}).get("wbi_img") or {})
                .get("sub_url") or "")
    img_key = img_url.split("/")[-1].split(".")[0] if img_url else ""
    sub_key = sub_url.split("/")[-1].split(".")[0] if sub_url else ""
    if not img_key or not sub_key:
        raise RuntimeError(
            "bilibili WBI keys missing — open bilibili.com in OpenSeer "
            "Chrome once so cookies + WBI state get populated.")

    raw = img_key + sub_key
    mixin_key = "".join(raw[i] if i < len(raw) else "" for i in _MIXIN_TABLE)[:32]

    import hashlib
    import time as _time
    wts = int(_time.time())
    sorted_params: dict[str, str] = {}
    for k in sorted({**params, "wts": str(wts)}):
        v = str({**params, "wts": str(wts)}[k])
        # Bilibili: strip these chars from values before signing
        v = re.sub(r"[!'()*]", "", v)
        sorted_params[k] = v
    # %20 for spaces (urlencode would emit +)
    import urllib.parse as up
    qs = "&".join(f"{up.quote(k, safe='')}={up.quote(v, safe='')}"
                   for k, v in sorted_params.items())
    w_rid = hashlib.md5((qs + mixin_key).encode("utf-8")).hexdigest()
    sorted_params["w_rid"] = w_rid
    return sorted_params


def _bili_api(tab: browser_cdp.CDPTab, path: str, params: dict,
              *, signed: bool = False) -> dict:
    """GET an api.bilibili.com endpoint through the page context.

    Raises RuntimeError when the JSON payload reports a non-zero
    `code` (signature reject, rate limit, auth required, …) so the
    site command surfaces a real error instead of silently
    returning "(no results)" — bilibili's error responses don't
    populate `data`, and the rows-from-payload mapping in each
    command would otherwise treat a failure as zero hits.
    """
    p = _wbi_sign(tab, params) if signed else {k: str(v) for k, v in params.items()}
    import urllib.parse as up
    qs = "&".join(f"{up.quote(k, safe='')}={up.quote(v, safe='')}"
                   for k, v in p.items())
    url = f"{_API_BASE}{path}?{qs}"
    url_js = json.dumps(url)
    js = f"""
    (async () => {{
      const r = await fetch({url_js}, {{ credentials: 'include' }});
      return await r.json();
    }})()
    """
    payload = _eval(tab, js) or {}
    if isinstance(payload, dict) and payload.get("code") not in (None, 0):
        msg = payload.get("message") or "unknown error"
        raise RuntimeError(
            f"Bilibili API {path} failed: {msg} (code={payload.get('code')})")
    return payload


# ─── commands ────────────────────────────────────────────────────────


class BiliSearch(SiteCommand):
    site = "bili"
    name = "search"
    description = "Search Bilibili videos or users"
    columns = ["rank", "title", "author", "score", "url"]

    def add_args(self, p: argparse.ArgumentParser) -> None:
        p.add_argument("query", help="Search keyword")
        p.add_argument("--type", choices=("video", "user"),
                        default="video",
                        help="What to search (default video)")
        p.add_argument("--page", type=int, default=1,
                        help="Result page (default 1)")
        p.add_argument("--limit", type=int, default=20,
                        help="Max results (default 20)")

    def run(self, args: argparse.Namespace) -> list[dict]:
        tab = _ensure_bili_tab()
        search_type = "bili_user" if args.type == "user" else "video"
        payload = _bili_api(tab,
            "/x/web-interface/wbi/search/type",
            {"search_type": search_type,
             "keyword": args.query, "page": args.page},
            signed=True)
        results = (payload.get("data") or {}).get("result") or []
        rows = []
        for i, item in enumerate(results[:args.limit]):
            if search_type == "bili_user":
                rows.append({
                    "rank": i + 1,
                    "title": _strip_html(item.get("uname") or ""),
                    "author": (item.get("usign") or "").strip(),
                    "score": item.get("fans") or 0,
                    "url": (f"https://space.bilibili.com/{item['mid']}"
                            if item.get("mid") else ""),
                })
            else:
                rows.append({
                    "rank": i + 1,
                    "title": _strip_html(item.get("title") or ""),
                    "author": item.get("author") or "",
                    "score": item.get("play") or 0,
                    "url": (f"https://www.bilibili.com/video/{item['bvid']}"
                            if item.get("bvid") else ""),
                })
        return rows


class BiliHot(SiteCommand):
    site = "bili"
    name = "hot"
    description = "Bilibili popular videos (热门)"
    columns = ["rank", "title", "author", "play", "danmaku", "bvid", "url"]

    def add_args(self, p: argparse.ArgumentParser) -> None:
        p.add_argument("--limit", type=int, default=20,
                        help="Max results (default 20)")

    def run(self, args: argparse.Namespace) -> list[dict]:
        tab = _ensure_bili_tab()
        # popular endpoint isn't WBI-signed
        payload = _bili_api(tab, "/x/web-interface/popular",
            {"ps": args.limit, "pn": 1})
        items = (payload.get("data") or {}).get("list") or []
        rows = []
        for i, item in enumerate(items[:args.limit]):
            owner = item.get("owner") or {}
            stat = item.get("stat") or {}
            bvid = item.get("bvid") or ""
            rows.append({
                "rank": i + 1,
                "title": item.get("title") or "",
                "author": owner.get("name") or "",
                "play": stat.get("view") or 0,
                "danmaku": stat.get("danmaku") or 0,
                "bvid": bvid,
                "url": (f"https://www.bilibili.com/video/{bvid}"
                        if bvid else ""),
            })
        return rows


class BiliVideo(SiteCommand):
    site = "bili"
    name = "video"
    description = "Get a Bilibili video's metadata (title, author, stats…)"
    columns = ["field", "value"]

    def add_args(self, p: argparse.ArgumentParser) -> None:
        p.add_argument("bvid",
                        help="BV ID, full bilibili.com URL, or b23.tv short link")

    def run(self, args: argparse.Namespace) -> list[dict]:
        bvid = self._resolve_bvid(args.bvid)
        tab = _ensure_bili_tab()
        # OpenCLI's video.js navigates to the video page first to
        # "prime the session" — we skip that because (a) _ensure_bili_tab
        # already landed us on bilibili.com so the cookie jar / WBI
        # state is populated, and (b) the SPA video page is heavy
        # (10s+) and we'd burn CDP's RPC timeout waiting for DOM
        # stability that's unnecessary for a bare /x/web-interface/view
        # call. The API endpoint doesn't require any referrer beyond
        # bilibili.com origin.
        payload = _bili_api(tab, "/x/web-interface/view", {"bvid": bvid})
        if payload.get("code") != 0:
            raise RuntimeError(
                f"Bilibili view API failed: {payload.get('message')} "
                f"({payload.get('code')})")
        d = payload.get("data") or {}
        stat = d.get("stat") or {}
        owner = d.get("owner") or {}
        from datetime import datetime, timezone
        pub = ""
        if d.get("pubdate"):
            pub = datetime.fromtimestamp(
                d["pubdate"], tz=timezone.utc).strftime("%Y-%m-%d %H:%M")
        dur = d.get("duration") or 0
        duration_text = f"{dur // 60}m{dur % 60}s ({dur}s)" if dur else ""

        fields = [
            ("bvid",         d.get("bvid") or ""),
            ("aid",          str(d.get("aid") or "")),
            ("title",        d.get("title") or ""),
            ("author",       (f"{owner.get('name')} (mid: {owner.get('mid')})"
                              if owner.get("name") else "")),
            ("category",     d.get("tname_v2") or d.get("tname") or ""),
            ("publish_time", pub),
            ("duration",     duration_text),
            ("view",         str(stat.get("view") or "")),
            ("danmaku",      str(stat.get("danmaku") or "")),
            ("reply",        str(stat.get("reply") or "")),
            ("like",         str(stat.get("like") or "")),
            ("coin",         str(stat.get("coin") or "")),
            ("favorite",     str(stat.get("favorite") or "")),
            ("share",        str(stat.get("share") or "")),
            ("thumbnail",    d.get("pic") or ""),
            ("description",  d.get("desc") or ""),
        ]
        return [{"field": k, "value": v} for k, v in fields]

    def _resolve_bvid(self, raw: str) -> str:
        """Accept three shapes (matches OpenCLI):
            1. Bare 'BV...' id
            2. Full bilibili.com/video/BV.../...
            3. b23.tv/XYZ short link
        For (3) we follow the HTTP redirect once."""
        s = (raw or "").strip()
        if re.match(r"^BV[A-Za-z0-9]+$", s, re.IGNORECASE):
            return s
        m = re.search(r"bilibili\.com/(?:video|bangumi/play)/(BV[A-Za-z0-9]+)",
                       s, re.IGNORECASE)
        if m:
            return m.group(1)
        # Short-link resolve: do a HEAD-style request, follow one redirect.
        short = re.sub(r"^https?://", "", s)
        short = re.sub(r"^(?:www\.)?b23\.tv/", "", short)
        import urllib.request
        url = f"https://b23.tv/{short}"
        try:
            with urllib.request.urlopen(url, timeout=5) as resp:
                final = resp.geturl()
        except Exception as e:
            raise RuntimeError(
                f"Cannot resolve BV ID from short URL: {raw!r}: {e}") from e
        m = re.search(r"/video/(BV[A-Za-z0-9]+)", final)
        if not m:
            raise RuntimeError(
                f"Cannot resolve BV ID from short URL: {raw!r} -> {final}")
        return m.group(1)


BiliSearch.register()
BiliHot.register()
BiliVideo.register()
