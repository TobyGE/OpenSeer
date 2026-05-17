"""Output formatting for site-CLI results.

Two modes:
  - `--json`: stdout gets a JSON array (one object per row)
  - default: pretty Markdown-style table (renders in CLI + chat)

The default is human-readable on purpose: the agent calls these via
bash and the table shows up in the action result string, which lands
in the agent's next-turn prompt. Markdown tables are something the
model already reads everywhere, so no extra parsing burden. If a
caller (script, jq pipe) actually wants structured data, they pass
`--json` and the model knows from the system prompt that the JSON
mode exists.
"""
from __future__ import annotations

import json
from typing import Any


def render(rows: list[dict[str, Any]], columns: list[str], *,
           as_json: bool = False) -> str:
    if as_json:
        return json.dumps(rows, ensure_ascii=False, indent=2)
    if not rows:
        return "(no results)"
    cols = columns or list(rows[0].keys())
    return _markdown_table(rows, cols)


def _markdown_table(rows: list[dict[str, Any]], cols: list[str]) -> str:
    # Stringify + truncate so a single huge cell doesn't blow up
    # the terminal. 80 chars is a soft cap that matches typical
    # terminal width while leaving room for adjacent columns.
    def cell(v: Any) -> str:
        s = "" if v is None else str(v)
        s = s.replace("\n", " ").replace("|", "\\|")
        return s if len(s) <= 80 else s[:77] + "..."

    widths = {c: max(len(c), *(len(cell(r.get(c))) for r in rows))
              for c in cols}
    head = "| " + " | ".join(c.ljust(widths[c]) for c in cols) + " |"
    sep = "|" + "|".join("-" * (widths[c] + 2) for c in cols) + "|"
    body = "\n".join(
        "| " + " | ".join(cell(r.get(c)).ljust(widths[c]) for c in cols) + " |"
        for r in rows
    )
    return f"{head}\n{sep}\n{body}"
