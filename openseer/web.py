"""Web search and page fetch — first-class agent tools.

`web_search(query)` returns ranked results (title + url + snippet) from
whichever provider has credentials in the environment. Backends, in
preference order:

  1. Tavily       (TAVILY_API_KEY)         — best signal, generous free tier
  2. Brave Search (BRAVE_SEARCH_API_KEY)   — fallback
  3. DuckDuckGo HTML (no key)              — last-resort, may rate-limit

`web_fetch(url)` returns the page as plain text (script/style stripped).

Both return strings shaped for the next-turn prompt: short, structured,
truncated. The model decides what to do with them; this module does
not interpret content.
"""
from __future__ import annotations

import json
import os
import re
import urllib.parse
import urllib.request


_DEFAULT_UA = "openseer/0.1 (+https://github.com/TobyGE/OpenSeer)"
_FETCH_BYTES_CAP = 1_500_000     # ~1.5MB raw; we'll truncate text further
_TEXT_CHARS_CAP = 4_000          # what the model actually sees


def _http_get(url: str, *, headers: dict | None = None,
              timeout: float = 10.0) -> tuple[int, bytes, str]:
    """Returns (status, body, content_type). On network failure (DNS, TLS,
    refused, timeout) returns (0, b"<error>: <msg>", "") so callers can
    surface the failure to the model instead of crashing the run."""
    req = urllib.request.Request(url, headers={
        "User-Agent": _DEFAULT_UA, **(headers or {}),
    })
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = resp.read(_FETCH_BYTES_CAP)
            ctype = resp.headers.get("Content-Type", "")
            return resp.status, data, ctype
    except urllib.error.HTTPError as e:
        body = b""
        try:
            body = e.read(_FETCH_BYTES_CAP) or b""
        except Exception:
            pass
        return e.code, body, e.headers.get("Content-Type", "") if e.headers else ""
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        return 0, f"network error: {e}".encode("utf-8", "replace"), ""


def _http_post_json(url: str, payload: dict, *, headers: dict | None = None,
                    timeout: float = 10.0) -> tuple[int, bytes]:
    """Returns (status, body). Status 0 indicates a non-HTTP network error
    whose message is in body."""
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=body, method="POST", headers={
        "User-Agent": _DEFAULT_UA,
        "Content-Type": "application/json",
        **(headers or {}),
    })
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read(_FETCH_BYTES_CAP)
    except urllib.error.HTTPError as e:
        return e.code, (e.read() or b"")
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        return 0, f"network error: {e}".encode("utf-8", "replace")


def _format_results(provider: str, items: list[dict]) -> str:
    if not items:
        return f"web_search[{provider}] — no results"
    lines = [f"web_search[{provider}] — {len(items)} result(s):"]
    for i, r in enumerate(items, 1):
        title = (r.get("title") or "").strip()[:200]
        url = (r.get("url") or "").strip()
        snippet = (r.get("snippet") or "").strip()
        snippet = re.sub(r"\s+", " ", snippet)[:300]
        lines.append(f"{i}. {title}\n   {url}\n   {snippet}")
    return "\n".join(lines)


def _search_tavily(query: str, *, count: int, freshness: str | None
                   ) -> tuple[str, str | None]:
    """Returns (result_text, error_text). Empty result_text + None error =
    "no credential, try next provider". Non-empty error_text = "provider
    failed, try next provider but surface this if everyone fails"."""
    key = os.environ.get("TAVILY_API_KEY", "").strip()
    if not key:
        return "", None
    payload: dict = {
        "api_key": key,
        "query": query,
        "max_results": count,
        "search_depth": "basic",
    }
    if freshness in ("day", "week", "month", "year"):
        # Tavily uses `time_range` for relative freshness filtering.
        payload["time_range"] = freshness
    code, body = _http_post_json("https://api.tavily.com/search", payload,
                                 timeout=12.0)
    if code != 200:
        return "", f"tavily HTTP {code}: {body[:200].decode('utf-8', 'replace')}"
    try:
        data = json.loads(body.decode("utf-8"))
    except Exception as e:
        return "", f"tavily parse error: {e}"
    items = [{"title": r.get("title"), "url": r.get("url"),
              "snippet": r.get("content")} for r in data.get("results", [])]
    return _format_results("tavily", items), None


def _search_brave(query: str, *, count: int, freshness: str | None
                  ) -> tuple[str, str | None]:
    key = os.environ.get("BRAVE_SEARCH_API_KEY", "").strip()
    if not key:
        return "", None
    params = {"q": query, "count": str(count)}
    fr_map = {"day": "pd", "week": "pw", "month": "pm", "year": "py"}
    if freshness in fr_map:
        params["freshness"] = fr_map[freshness]
    url = "https://api.search.brave.com/res/v1/web/search?" + \
          urllib.parse.urlencode(params)
    code, body, _ = _http_get(url, headers={
        "Accept": "application/json",
        "X-Subscription-Token": key,
    }, timeout=12.0)
    if code != 200:
        return "", f"brave HTTP {code}: {body[:200].decode('utf-8', 'replace')}"
    try:
        data = json.loads(body.decode("utf-8"))
    except Exception as e:
        return "", f"brave parse error: {e}"
    web = (data.get("web") or {}).get("results") or []
    items = [{"title": r.get("title"), "url": r.get("url"),
              "snippet": r.get("description")} for r in web]
    return _format_results("brave", items), None


_DDG_RESULT_RE = re.compile(
    r'<a[^>]+class="result__a"[^>]+href="([^"]+)"[^>]*>(.*?)</a>'
    r'.*?<a[^>]+class="result__snippet"[^>]*>(.*?)</a>',
    re.S,
)


def _strip_html(s: str) -> str:
    s = re.sub(r"<[^>]+>", "", s)
    return re.sub(r"\s+", " ", s).strip()


def _search_ddg(query: str, *, count: int) -> str:
    """DuckDuckGo HTML endpoint — no key, may rate-limit."""
    url = "https://html.duckduckgo.com/html/?" + \
          urllib.parse.urlencode({"q": query})
    code, body, _ = _http_get(url, timeout=12.0)
    if code != 200:
        return (f"web_search[ddg] HTTP {code} — set TAVILY_API_KEY or "
                f"BRAVE_SEARCH_API_KEY for reliable search")
    html = body.decode("utf-8", "replace")
    items = []
    for m in _DDG_RESULT_RE.finditer(html):
        href, title, snippet = m.group(1), m.group(2), m.group(3)
        # DDG link wraps the real URL in a redirect: /l/?uddg=<encoded>
        if href.startswith("//"):
            href = "https:" + href
        if "uddg=" in href:
            try:
                qs = urllib.parse.urlparse(href).query
                href = urllib.parse.parse_qs(qs).get("uddg", [href])[0]
            except Exception:
                pass
        items.append({
            "title": _strip_html(title),
            "url": href,
            "snippet": _strip_html(snippet),
        })
        if len(items) >= count:
            break
    return _format_results("ddg", items)


def web_search(query: str, *, count: int = 5,
               freshness: str | None = None) -> str:
    """Run a web search and return ranked results as plain text.

    Tries providers in order. Skips providers without credentials. If a
    configured provider errors (rate limit, invalid key, parse failure),
    falls through to the next provider. If every provider fails, returns
    the chain of errors so the caller sees what went wrong.
    """
    query = (query or "").strip()
    if not query:
        return "ERROR: web_search needs `query`"
    count = max(1, min(20, int(count)))
    errors: list[str] = []
    for fn in (_search_tavily, _search_brave):
        result, err = fn(query, count=count, freshness=freshness)
        if result:
            return result
        if err:
            errors.append(err)
    # DDG as last-resort, no credential needed.
    ddg = _search_ddg(query, count=count)
    if ddg.startswith("web_search[ddg] —"):
        return ddg
    errors.append(ddg)
    return "web_search: all providers failed\n" + "\n".join(errors)


def web_fetch(url: str) -> str:
    """Fetch a URL and return its text content (HTML stripped)."""
    url = (url or "").strip()
    if not url:
        return "ERROR: web_fetch needs `url`"
    if not re.match(r"^https?://", url, re.I):
        url = "https://" + url
    code, body, ctype = _http_get(url, timeout=15.0)
    if code >= 400:
        return f"web_fetch[{code}] {url}\n{body[:500].decode('utf-8', 'replace')}"
    text = body.decode("utf-8", "replace")
    if "html" in (ctype or "").lower() or text.lstrip().startswith("<"):
        text = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", text,
                      flags=re.S | re.I)
        text = re.sub(r"<[^>]+>", " ", text)
        text = re.sub(r"\s+", " ", text).strip()
    head = f"web_fetch[{code}] {url} ({len(body)} bytes)"
    if len(text) > _TEXT_CHARS_CAP:
        text = text[:_TEXT_CHARS_CAP] + f"\n…[truncated, {len(text)} chars total]"
    return f"{head}\n{text}"
