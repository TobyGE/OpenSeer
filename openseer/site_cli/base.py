"""SiteCommand base class + module-level registry.

The registry is `{site_name: {cmd_name: SiteCommand}}`. Site modules
register themselves at import time by instantiating SiteCommand
subclasses with `register=True`.
"""
from __future__ import annotations

import argparse
from typing import Any


# Registry of site → cmd → handler. Mutated at site-module import time.
registry: dict[str, dict[str, "SiteCommand"]] = {}


class SiteCommand:
    """One command under one site, e.g. `arxiv search` or `bili hot`.

    Subclasses set the class attrs and implement `run(args) -> list[dict]`.
    The dispatcher (in `openseer.cli`) wires argparse, calls `run`, and
    formats the returned rows via `output.render`.

    Why list[dict] instead of a richer schema: matches OpenCLI's
    `{columns, rows}` shape, plays nicely with both table and JSON
    output, and is what the model actually needs to consume the
    result through bash + jq.
    """

    site: str = ""              # e.g. "arxiv"
    name: str = ""              # e.g. "search"
    description: str = ""       # one-line, shown in --help
    columns: list[str] = []     # display columns for table output
    needs_browser: bool = True  # if False, skip CDP launch

    def add_args(self, p: argparse.ArgumentParser) -> None:
        """Subclasses add their argparse args here."""

    def run(self, args: argparse.Namespace) -> list[dict]:
        """Run the command. Return a list of row dicts whose keys
        are a subset/superset of `columns`."""
        raise NotImplementedError

    @classmethod
    def register(cls) -> "SiteCommand":
        """Instantiate + register this command under (site, name)."""
        inst = cls()
        registry.setdefault(cls.site, {})[cls.name] = inst
        return inst
