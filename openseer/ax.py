"""Compatibility shim — the AX wrapper now lives in its own
top-level package (`openseer_ax`) so other projects can use it
without pulling in the rest of OpenSeer.

Existing imports keep working:
    from openseer.ax import dump_ax_tree, AXElem, ...

New code should prefer:
    from openseer_ax import dump_ax_tree, AXElem, ...

Caveat about mutable module state (HOST_TERMINAL_PIDS): re-binding
attributes on this shim (e.g. `openseer.ax.HOST_TERMINAL_PIDS = X`)
will NOT propagate to the canonical `openseer_ax` module that
`render_ax_for_prompt` reads from. Callers that need to mutate
shared module state must import `openseer_ax` directly.
"""
from openseer_ax import (  # noqa: F401  re-export
    AXElem,
    active_app_pid,
    app_pid_by_name,
    dump_ax_tree,
    render_ax_for_prompt,
)
# Re-export a few private names that internal-ish callers may rely
# on. `from openseer_ax import *` below would skip these since they
# start with an underscore. Keep this list minimal and prefer
# importing from `openseer_ax` directly in new code.
from openseer_ax import (  # noqa: F401  re-export private helpers
    _AX_AVAILABLE,
    _terminal_app_pids_in_ancestry,
)
# Also re-export everything else (role sets, public helpers, etc.)
# for callers that did `from openseer.ax import *`.
from openseer_ax import *  # noqa: F401,F403
