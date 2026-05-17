"""arXiv site CLI — `openseer site arxiv {search|paper|recent}`.

Ported from OpenCLI/clis/arxiv/ (https://github.com/JackWener/OpenCLI,
Apache 2.0). arXiv exposes a public Atom/XML query API at
`export.arxiv.org/api/query` so this site is `needs_browser=False`
— no CDP launch, just urllib + a tiny XML parser.

Layout mirrors OpenCLI's three commands:
    search <query> [--limit N]
    paper <id>
    recent --category cs.CL [--limit N]
"""
from __future__ import annotations

import argparse
import re
import urllib.parse
import urllib.request

from .base import SiteCommand


_ARXIV_BASE = "https://export.arxiv.org/api/query"
_USER_AGENT = "openseer-site-cli/0.1 (+https://github.com/TobyGE/OpenSeer)"
# Categories are e.g. cs.CL, math.PR, q-bio.NC, physics.comp-ph.
# Reject anything else early so we don't ship a malformed query to
# arxiv (which would just 200 with an empty feed and we'd raise a
# confusing "no results" instead of a clean validation error).
_CATEGORY_RE = re.compile(
    r"^[a-z]+(?:-[a-z]+)*(?:\.[A-Za-z0-9]+(?:-[A-Za-z0-9]+)*)?$")


def _arxiv_fetch(qs: str) -> str:
    url = f"{_ARXIV_BASE}?{qs}"
    req = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
    # 30s timeout — keyword search returns in <2s, but author
    # phrase queries (`au:"Yoshua Bengio"`) routinely take 10-25s
    # on arxiv's public endpoint. 15s wasn't enough for that path
    # under normal load.
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        if e.code == 429:
            raise RuntimeError(
                "arXiv API rate-limited (HTTP 429). Wait ~10s and "
                "retry; their public API rate-limits per IP.") from e
        raise RuntimeError(
            f"arXiv API HTTP {e.code}: check your search term or paper ID")\
            from e
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        # Transport-level failures (DNS, refused connection, offline,
        # urllib's wrapped socket timeout). Wrap as RuntimeError so
        # the dispatcher's clean error path takes over instead of
        # leaking a traceback to the user / agent.
        raise RuntimeError(
            f"arXiv API unreachable: {type(e).__name__}: {e}") from e


# ─── tiny XML helpers (Atom feed, fixed shape) ────────────────────────
# arXiv's API returns predictable, well-formed XML. Pulling in
# lxml/ElementTree just for a known feed shape is overkill; the
# regex approach matches OpenCLI's port and is ~30 lines.

def _decode_entities(s: str) -> str:
    return (s.replace("&amp;", "&").replace("&lt;", "<")
             .replace("&gt;", ">").replace("&quot;", '"')
             .replace("&apos;", "'").replace("&#39;", "'"))


def _extract(xml: str, tag: str) -> str:
    m = re.search(rf"<{tag}[^>]*>([\s\S]*?)</{tag}>", xml)
    return m.group(1).strip() if m else ""


def _extract_all(xml: str, tag: str) -> list[str]:
    return [m.group(1).strip()
            for m in re.finditer(rf"<{tag}[^>]*>([\s\S]*?)</{tag}>", xml)]


def _extract_attr(xml: str, tag: str, attr: str) -> str:
    m = re.search(rf"<{tag}\b[^>]*?\b{attr}=\"([^\"]*)\"", xml)
    return m.group(1) if m else ""


def _extract_all_attr(xml: str, tag: str, attr: str) -> list[str]:
    return [m.group(1) for m in re.finditer(
        rf"<{tag}\b[^>]*?\b{attr}=\"([^\"]*)\"", xml)]


def _find_link_href(xml: str, rel: str) -> str:
    for m in re.finditer(r"<link\b([^>]*)/?>", xml):
        attrs = m.group(1)
        if re.search(rf'\brel="{rel}"', attrs):
            h = re.search(r'\bhref="([^"]*)"', attrs)
            if h:
                return h.group(1)
    return ""


def _parse_entries(xml: str) -> list[dict]:
    entries = []
    for m in re.finditer(r"<entry>([\s\S]*?)</entry>", xml):
        e = m.group(1)
        raw_id = _extract(e, "id")
        arxiv_id = re.sub(r"^https?://arxiv\.org/abs/", "", raw_id)
        arxiv_id = re.sub(r"v\d+$", "", arxiv_id)
        pdf = _find_link_href(e, "related") \
            or f"https://arxiv.org/pdf/{arxiv_id}"
        entries.append({
            "id": arxiv_id,
            "title": _decode_entities(re.sub(r"\s+", " ", _extract(e, "title"))),
            "authors": _decode_entities(", ".join(_extract_all(e, "name"))),
            "abstract": _decode_entities(
                re.sub(r"\s+", " ", _extract(e, "summary"))),
            "published": _extract(e, "published")[:10],
            "updated": _extract(e, "updated")[:10],
            "primary_category": _extract_attr(
                e, "arxiv:primary_category", "term"),
            "categories": ", ".join(_extract_all_attr(e, "category", "term")),
            "comment": _decode_entities(
                re.sub(r"\s+", " ", _extract(e, "arxiv:comment"))),
            "pdf": pdf,
            "url": f"https://arxiv.org/abs/{arxiv_id}",
        })
    return entries


def _check_limit(value: int, *, default: int, max_value: int,
                 label: str = "limit") -> int:
    n = int(value) if value is not None else default
    if n <= 0:
        raise ValueError(f"arxiv {label} must be a positive integer")
    if n > max_value:
        raise ValueError(f"arxiv {label} must be <= {max_value}")
    return n


# ─── commands ────────────────────────────────────────────────────────


class ArxivSearch(SiteCommand):
    site = "arxiv"
    name = "search"
    description = "Search arXiv papers by keyword"
    columns = ["id", "title", "authors", "published",
                "primary_category", "url"]
    needs_browser = False

    def add_args(self, p: argparse.ArgumentParser) -> None:
        p.add_argument("query", help="Search keyword (e.g. 'attention is all you need')")
        p.add_argument("--limit", type=int, default=10,
                        help="Max results (default 10, max 25)")

    def run(self, args: argparse.Namespace) -> list[dict]:
        q = (args.query or "").strip()
        if not q:
            raise ValueError("arxiv search query cannot be empty")
        limit = _check_limit(args.limit, default=10, max_value=25)
        qs = (f"search_query={urllib.parse.quote('all:' + q)}"
              f"&max_results={limit}&sortBy=relevance")
        entries = _parse_entries(_arxiv_fetch(qs))
        if not entries:
            raise RuntimeError(
                f"No arXiv papers found for {q!r}. Try a different keyword.")
        # Trim to declared columns so the table is clean; the JSON
        # output also benefits — full abstract is huge and not what
        # search results want anyway (paper command has it).
        return [{k: e[k] for k in self.columns} for e in entries]


class ArxivPaper(SiteCommand):
    site = "arxiv"
    name = "paper"
    description = "Get arXiv paper details by ID"
    columns = ["id", "title", "authors", "published", "updated",
                "primary_category", "categories", "abstract",
                "comment", "pdf", "url"]
    needs_browser = False

    def add_args(self, p: argparse.ArgumentParser) -> None:
        p.add_argument("id", help="arXiv paper ID (e.g. 1706.03762)")

    def run(self, args: argparse.Namespace) -> list[dict]:
        qs = f"id_list={urllib.parse.quote(args.id)}"
        entries = _parse_entries(_arxiv_fetch(qs))
        if not entries:
            raise RuntimeError(
                f"arXiv paper {args.id!r} not found. Check the ID format, "
                f"e.g. 1706.03762.")
        return [{k: e[k] for k in self.columns} for e in entries]


class ArxivAuthor(SiteCommand):
    site = "arxiv"
    name = "author"
    description = "List arXiv papers by a given author (newest first)"
    columns = ["id", "title", "authors", "published",
                "primary_category", "url"]
    needs_browser = False

    def add_args(self, p: argparse.ArgumentParser) -> None:
        p.add_argument("author",
                        help='Author name (e.g. "Yoshua Bengio" or "Y Bengio")')
        p.add_argument("--limit", type=int, default=20,
                        help="Max papers (default 20, max 50)")

    def run(self, args: argparse.Namespace) -> list[dict]:
        # Author names on arXiv aren't stable IDs — same person
        # often appears under multiple spellings ("Y. Bengio" vs
        # "Yoshua Bengio"). Quote the value so multi-word names
        # match as a phrase rather than as separate terms.
        author = (args.author or "").strip()
        if not author:
            raise ValueError(
                'arxiv author cannot be empty. Example: '
                'openseer site arxiv author "Yoshua Bengio"')
        limit = _check_limit(args.limit, default=20, max_value=50)
        q = urllib.parse.quote(f'au:"{author}"')
        qs = (f"search_query={q}&max_results={limit}"
              f"&sortBy=submittedDate&sortOrder=descending")
        entries = _parse_entries(_arxiv_fetch(qs))
        if not entries:
            raise RuntimeError(
                f"No arXiv papers found for author {author!r}. "
                f"Try alternate spellings (e.g. initials).")
        return [{k: e[k] for k in self.columns} for e in entries]


class ArxivRecent(SiteCommand):
    site = "arxiv"
    name = "recent"
    description = "Recent arXiv papers in a category"
    columns = ["id", "title", "authors", "published",
                "primary_category", "url"]
    needs_browser = False

    def add_args(self, p: argparse.ArgumentParser) -> None:
        p.add_argument("--category", required=True,
                        help="arXiv category, e.g. cs.CL, cs.LG, math.PR")
        p.add_argument("--limit", type=int, default=10,
                        help="Max results (default 10, max 25)")

    def run(self, args: argparse.Namespace) -> list[dict]:
        cat = (args.category or "").strip()
        if not _CATEGORY_RE.match(cat):
            raise ValueError(
                f"Invalid arXiv category {cat!r}. Examples: cs.CL, cs.LG, "
                f"math.PR, q-bio.NC, physics.comp-ph")
        limit = _check_limit(args.limit, default=10, max_value=25)
        qs = (f"search_query=cat:{urllib.parse.quote(cat)}"
              f"&max_results={limit}&sortBy=submittedDate&sortOrder=descending")
        entries = _parse_entries(_arxiv_fetch(qs))
        if not entries:
            raise RuntimeError(
                f"No recent arXiv papers found in {cat!r}.")
        return [{k: e[k] for k in self.columns} for e in entries]


# Register at import-time. The dispatcher's _registry() triggers this
# via `from . import arxiv`.
ArxivSearch.register()
ArxivPaper.register()
ArxivAuthor.register()
ArxivRecent.register()
