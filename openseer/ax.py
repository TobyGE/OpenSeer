"""Compatibility shim — the AX wrapper now lives in its own
top-level package (`openseer_ax`) so other projects can use it
without pulling in the rest of OpenSeer.

Existing imports keep working:
    from openseer.ax import dump_ax_tree, AXElem, ...

New code should prefer:
    from openseer_ax import dump_ax_tree, AXElem, ...
"""
from openseer_ax import (  # noqa: F401  re-export
    AXElem,
    active_app_pid,
    app_pid_by_name,
    dump_ax_tree,
    render_ax_for_prompt,
)
# Also re-export everything else (private helpers, role sets, etc.)
# for callers that did `from openseer.ax import *`.
from openseer_ax import *  # noqa: F401,F403
