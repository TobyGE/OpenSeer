"""Per-site mini-CLIs that drive the OpenSeer Chrome via CDP.

The agent calls them through bash:

    openseer site arxiv search "attention is all you need" --limit 5
    openseer site bili search "lofi" --limit 10 --json
    openseer site bili video BV1xxx

One `openseer site …` invocation replaces N rounds of read_page +
click — the work is deterministic, the result is structured (table
for humans, JSON for the model / pipelines), and there's no
selector to break on the next site rev.

Architecture ported / borrowed from OpenCLI (https://github.com/
JackWener/OpenCLI, Apache 2.0). OpenCLI's design insight is that
"browser CLIs" are mostly logged-in fetch wrappers: each site
command is `page.evaluate("fetch(api, {credentials:'include'})")`
rather than DOM scraping. That maps 1:1 onto our existing
`CDPTab.evaluate` from `openseer/browser_cdp.py` — we don't need
their `base-page` abstraction; we have one.

Adding a new site (cookie-recipe):

    1. New module `openseer/site_cli/<site>.py`
    2. Subclass SiteCommand for each command
    3. Register the site in `_registry()` at the bottom of this file
    4. (Header comment: name the OpenCLI source path you ported from)
"""
from __future__ import annotations

from .base import SiteCommand, registry  # re-export


def _registry() -> dict[str, dict[str, SiteCommand]]:
    """Lazy-import each site module so a broken port can't crash
    the whole `openseer site` subcommand. Each module's import-time
    side effect is to register itself via the `registry` dict the
    base module owns."""
    from . import arxiv as _arxiv     # noqa: F401  side-effecting import
    from . import bilibili as _bili   # noqa: F401
    from . import reddit as _reddit   # noqa: F401
    return registry


__all__ = ["SiteCommand", "_registry"]
