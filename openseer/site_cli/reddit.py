"""Reddit site CLI — `openseer site reddit {hot|popular|search}`.

Ported from OpenCLI/clis/reddit/{hot,popular,search}.js
(https://github.com/JackWener/OpenCLI, Apache 2.0).

Reddit exposes JSON variants of its web routes (`/hot.json`,
`/r/popular.json`, `/search.json`) that work anonymously — no
login required for reads. We still go through CDP rather than
direct urllib so the request runs in the page's same-origin
context with `credentials: 'include'`, which lets Reddit's
session cookies (when present) personalize the result for a
logged-in user. Anonymous users get the same shape, just
without personalization.

Why this is a good "Chrome actually opens" demo:
  - Reddit's web client renders entirely client-side, so a fresh
    visit definitely spins up renderer threads, and you'll see
    the OpenSeer Chrome window flash a tab into view briefly.
  - The data path stays clean — page.evaluate(fetch(...)) returns
    structured JSON we map into rows.
"""
from __future__ import annotations

import argparse
import json
import re
from typing import Any

from .. import browser_cdp
from .base import SiteCommand


# ─── helpers ─────────────────────────────────────────────────────────


def _normalize_subreddit(raw: str) -> str:
    """Accept 'rust', '/r/rust', 'r/rust' → 'rust'. Crucially NOT
    `lstrip('r/')` which is a CHAR-SET strip (would turn 'rust'
    into 'ust', 'relationships' into 'elationships', etc.).
    Codex P2 on first reddit push."""
    s = (raw or "").strip()
    if s.startswith("/r/"):
        s = s[3:]
    elif s.startswith("r/"):
        s = s[2:]
    return s.strip("/")


def _ensure_reddit_tab() -> browser_cdp.CDPTab:
    """Mirror of bilibili's _ensure_bili_tab — return a CDPTab
    bound to a reddit.com page (so the subsequent in-page fetch is
    same-origin and carries any session cookies). Reuses an
    existing reddit tab when possible; otherwise opens a fresh
    one alongside any unrelated tabs (never closes them)."""
    mgr = browser_cdp.ChromeManager.shared()
    tab = mgr.front_tab()
    if tab is not None:
        url = ""
        try:
            url = browser_cdp._BackgroundLoop.shared().run(
                tab.current_url(), timeout=3.0) or ""
        except Exception:
            pass
        # Acceptable: reddit.com home or any /r/<sub> page (no
        # post-detail / message / submit forms — those JS evaluate
        # contexts can be heavy and unrelated to the API call).
        # Host check stays strict so we don't accept e.g.
        # google.com/search?q=reddit by accident.
        is_reddit_host = bool(re.match(
            r"^https?://(?:www\.|old\.|new\.)?reddit\.com(?:[/?#]|$)", url))
        is_reddit_home_or_sub = bool(re.match(
            r"^https?://(?:www\.|old\.|new\.)?reddit\.com/?"
            r"(?:r/[^/?#]+/?)?(?:\?|#|$)", url))
        if is_reddit_host and is_reddit_home_or_sub:
            return tab
        # Tab unsuitable — drop our local websocket (does NOT close
        # the OS tab; ChromeManager.open_tab will create a fresh
        # target alongside it). Same lifecycle pattern bilibili.py
        # established and codex review reviewed.
        try:
            browser_cdp._BackgroundLoop.shared().run(
                tab.close(), timeout=2.0)
        except Exception:
            pass

    new_tab = mgr.open_tab("https://www.reddit.com")
    if new_tab.target_id:
        mgr.remember_target(new_tab.target_id)

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
        pass
    return new_tab


def _reddit_fetch(tab: browser_cdp.CDPTab, path: str) -> dict:
    """GET a reddit.com JSON endpoint from inside the page context.

    Reddit's anti-bot rate-limits direct urllib hits hard (often
    429) but is tolerant of in-browser fetches because they look
    like normal client-side navigation. Routing through CDP also
    means a logged-in cookie jar (if the user signed in inside
    OpenSeer Chrome) automatically personalizes results.

    Returns the parsed JSON payload OR raises RuntimeError when
    Reddit signals a failure — HTTP non-2xx, error envelope
    (`{error: 403, message: "Forbidden"}`), or no listing data.
    Without the raise a banned/private subreddit or rate-limit
    would silently render as "(no results)", which makes a real
    failure look like legitimately empty data.
    """
    url_js = json.dumps(path)
    # Capture HTTP status alongside body so we can distinguish
    # transport errors (429, 5xx) from API-level errors (200 +
    # error JSON, which reddit also does).
    js = f"""
    (async () => {{
      const r = await fetch({url_js}, {{ credentials: 'include' }});
      let body = null;
      try {{ body = await r.json(); }} catch (e) {{ body = null; }}
      return {{ status: r.status, body }};
    }})()
    """
    # Route through _run_cdp so any leaked websockets / OSError /
    # asyncio.TimeoutError becomes CDPError, which the cli
    # dispatcher already catches. Without this wrap a stale tab
    # (cached from a prior run, websocket gone) would crash with
    # a `ConnectionClosedError` traceback.
    result = browser_cdp._run_cdp(
        lambda: tab.evaluate(js, await_promise=True),
        what=f"reddit {path}")
    if not isinstance(result, dict):
        raise RuntimeError(
            f"Reddit returned a non-JSON payload for {path!r}")
    status = result.get("status")
    payload = result.get("body")
    if not isinstance(status, int) or status < 200 or status >= 300:
        # Surface as a clean error string. Special-case 429 because
        # the suggested wait is what unblocks the user.
        if status == 429:
            raise RuntimeError(
                f"Reddit API rate-limited (HTTP 429) for {path!r}. "
                f"Wait ~10s and retry.")
        if status in (403, 404):
            raise RuntimeError(
                f"Reddit API HTTP {status} for {path!r} "
                f"— subreddit may be private, banned, or misspelled.")
        raise RuntimeError(
            f"Reddit API HTTP {status} for {path!r}")
    if not isinstance(payload, dict):
        raise RuntimeError(
            f"Reddit returned a non-JSON body for {path!r}")
    # Reddit's API also packs errors into a 200 OK body sometimes,
    # e.g. `{"error": 429, "message": "Too Many Requests"}`. Treat
    # an `error` field as failure.
    if "error" in payload:
        raise RuntimeError(
            f"Reddit API error for {path!r}: "
            f"{payload.get('message') or payload.get('error')}")
    return payload


def _children_to_rows(payload: dict, *, with_rank: bool = True,
                       with_post_id: bool = False) -> list[dict]:
    children = ((payload.get("data") or {}).get("children") or [])
    rows: list[dict] = []
    for i, c in enumerate(children):
        d = c.get("data") or {}
        row = {
            "title": d.get("title") or "",
            "subreddit": d.get("subreddit_name_prefixed") or "",
            "score": d.get("score") or 0,
            "comments": d.get("num_comments") or 0,
            "author": d.get("author") or "",
            "url": ("https://www.reddit.com" + (d.get("permalink") or "")
                    if d.get("permalink") else ""),
        }
        if with_post_id:
            row["postId"] = d.get("id") or ""
        if with_rank:
            row = {"rank": i + 1, **row}
        rows.append(row)
    return rows


# ─── commands ────────────────────────────────────────────────────────


class RedditHot(SiteCommand):
    site = "reddit"
    name = "hot"
    description = "Reddit hot posts — frontpage or a specific subreddit"
    columns = ["rank", "title", "subreddit", "score", "comments",
                "postId", "author", "url"]

    def add_args(self, p: argparse.ArgumentParser) -> None:
        p.add_argument("--subreddit", default="",
                        help='Subreddit name (e.g. "programming"). '
                             "Empty for frontpage hot.")
        p.add_argument("--limit", type=int, default=20,
                        help="Number of posts (default 20)")

    def run(self, args: argparse.Namespace) -> list[dict]:
        sub = _normalize_subreddit(args.subreddit)
        path = (f"/r/{sub}/hot.json" if sub else "/hot.json")
        path += f"?limit={int(args.limit)}&raw_json=1"
        tab = _ensure_reddit_tab()
        payload = _reddit_fetch(tab, path)
        rows = _children_to_rows(payload, with_post_id=True)
        return rows[:int(args.limit)]


class RedditPopular(SiteCommand):
    site = "reddit"
    name = "popular"
    description = "Reddit /r/popular — site-wide trending"
    columns = ["rank", "title", "subreddit", "score", "comments", "url"]

    def add_args(self, p: argparse.ArgumentParser) -> None:
        p.add_argument("--limit", type=int, default=20,
                        help="Number of posts (default 20)")

    def run(self, args: argparse.Namespace) -> list[dict]:
        path = f"/r/popular.json?limit={int(args.limit)}&raw_json=1"
        tab = _ensure_reddit_tab()
        payload = _reddit_fetch(tab, path)
        return _children_to_rows(payload)[:int(args.limit)]


class RedditSearch(SiteCommand):
    site = "reddit"
    name = "search"
    description = "Search Reddit posts (site-wide or within a subreddit)"
    columns = ["title", "subreddit", "author", "score", "comments", "url"]

    def add_args(self, p: argparse.ArgumentParser) -> None:
        p.add_argument("query", help="Reddit search query")
        p.add_argument("--subreddit", default="",
                        help="Search within a specific subreddit")
        p.add_argument("--sort", default="relevance",
                        choices=("relevance", "hot", "top", "new", "comments"),
                        help="Sort order (default relevance)")
        p.add_argument("--time", default="all",
                        choices=("hour", "day", "week", "month", "year", "all"),
                        help="Time filter (default all)")
        p.add_argument("--limit", type=int, default=15,
                        help="Number of results (default 15)")

    def run(self, args: argparse.Namespace) -> list[dict]:
        import urllib.parse
        q = urllib.parse.quote(args.query)
        sub = _normalize_subreddit(args.subreddit)
        base = f"/r/{sub}/search.json" if sub else "/search.json"
        params = (f"q={q}&sort={args.sort}&t={args.time}"
                  f"&limit={int(args.limit)}"
                  f"&restrict_sr={'on' if sub else 'off'}&raw_json=1")
        path = f"{base}?{params}"
        tab = _ensure_reddit_tab()
        payload = _reddit_fetch(tab, path)
        rows = _children_to_rows(payload, with_rank=False)
        # search results don't need the postId column; drop it
        # so output stays compact.
        for r in rows:
            r.pop("postId", None)
        return rows[:int(args.limit)]


RedditHot.register()
RedditPopular.register()
RedditSearch.register()
