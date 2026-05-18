"""macOS Accessibility-tree wrapper. Apache 2.0.

Pulls the AX tree of the frontmost app (or a named one), filters down to
interactive elements with labels + bboxes, and exposes them as an indexed
list a computer-use agent can click by index instead of pixel coordinates.

Originally lived inside the OpenSeer monorepo as `openseer.ax`; lifted out
into its own top-level package so other macOS automation projects can use
it without depending on OpenSeer itself (and so OpenSeer can later move
this out to its own GitHub repo + PyPI release without touching internal
callers). External users:

    pip install openseer-ax  # once we publish

    from openseer_ax import dump_ax_tree, active_app_pid, render_ax_for_prompt

The legacy `from openseer.ax import …` path is kept as a re-export shim
so existing code doesn't break.

Catalyst apps need an explicit messaging timeout bump: stock 2s often
returns empty trees, 5s makes them work. We do that on every app element.

Public API:
  - dump_ax_tree(pid=None) → list[AXElem]
  - active_app_pid() → int | None
  - app_pid_by_name(name) → int | None
  - render_ax_for_prompt(elems, max_lines=...) → str
"""
from __future__ import annotations

from dataclasses import dataclass

try:
    from AppKit import NSWorkspace  # type: ignore[import-untyped]
    from ApplicationServices import (  # type: ignore[import-untyped]
        AXUIElementCreateApplication,
        AXUIElementCopyAttributeValue,
        AXUIElementSetAttributeValue,
        AXUIElementIsAttributeSettable,
        AXUIElementCopyActionNames,
        AXUIElementPerformAction,
        AXUIElementSetMessagingTimeout,
        AXValueGetValue,
        kAXValueCGPointType,
        kAXValueCGSizeType,
    )
    _AX_AVAILABLE = True
except Exception:                         # pragma: no cover - non-mac envs
    _AX_AVAILABLE = False

try:
    from Quartz import (                # type: ignore[import-untyped]
        CGWindowListCopyWindowInfo,
        kCGWindowListOptionOnScreenOnly,
        kCGNullWindowID,
    )
    _CG_AVAILABLE = True
except Exception:
    _CG_AVAILABLE = False



# AX roles that are worth surfacing to the model. AXGroup et al. are
# layout-only and contribute noise without leaf labels.
_INTERACTIVE_ROLES = frozenset({
    "AXButton", "AXRadioButton", "AXCheckBox", "AXPopUpButton",
    "AXMenuItem", "AXMenuButton", "AXTextField", "AXTextArea",
    "AXSearchField", "AXLink", "AXComboBox", "AXIncrementor",
    "AXSlider", "AXStepper", "AXTab", "AXTabGroup", "AXDisclosureTriangle",
    "AXSegmentedControl", "AXOutlineRow", "AXRow", "AXCell",
    "AXImage", "AXScrollBar",
})

# Static text often contains the only useful label (e.g. status / count
# strings). Keep them but mark separately so prompt rendering can show
# them as context, not as click targets.
_STATIC_ROLES = frozenset({
    "AXStaticText", "AXValueIndicator", "AXLevelIndicator",
})


@dataclass
class AXElem:
    idx: int                # flat index, what the model will click by
    role: str
    label: str
    bbox: tuple[int, int, int, int] | None    # (x, y, w, h) in screen coords
    depth: int
    interactive: bool       # True = Button etc., False = StaticText etc.

    @property
    def center(self) -> tuple[int, int] | None:
        if self.bbox is None:
            return None
        x, y, w, h = self.bbox
        return (x + w // 2, y + h // 2)


def _attr(elem, name: str):
    if not _AX_AVAILABLE:
        return None
    err, val = AXUIElementCopyAttributeValue(elem, name, None)
    return val if err == 0 else None


def _struct_xy(p) -> tuple[float, float] | None:
    """PyObjC may bridge a CGPoint to either an attribute-style object or a
    tuple-like one depending on framework version. Try both."""
    if p is None:
        return None
    # attribute access (most macOS PyObjC builds)
    try:
        return (float(p.x), float(p.y))
    except Exception:
        pass
    # tuple-style indexing (some bridge versions)
    try:
        return (float(p[0]), float(p[1]))
    except Exception:
        return None


def _struct_wh(s) -> tuple[float, float] | None:
    if s is None:
        return None
    try:
        return (float(s.width), float(s.height))
    except Exception:
        pass
    try:
        return (float(s[0]), float(s[1]))
    except Exception:
        return None


def _unbox_pos(v) -> tuple[float, float] | None:
    if v is None:
        return None
    direct = _struct_xy(v)
    if direct is not None:
        return direct
    try:
        ok = AXValueGetValue(v, kAXValueCGPointType, None)
        if ok and len(ok) > 1 and ok[1] is not None:
            return _struct_xy(ok[1])
    except Exception:
        pass
    return None


def _unbox_size(v) -> tuple[float, float] | None:
    if v is None:
        return None
    direct = _struct_wh(v)
    if direct is not None:
        return direct
    try:
        ok = AXValueGetValue(v, kAXValueCGSizeType, None)
        if ok and len(ok) > 1 and ok[1] is not None:
            return _struct_wh(ok[1])
    except Exception:
        pass
    return None


def app_pid_by_name(name: str) -> int | None:
    """Resolve an app name to a pid. Two-pass to avoid picking helper
    processes (e.g. "Google Chrome Helper (GPU)" containing "chrome"):

      1. exact localized-name match, regular activation policy only
      2. exact bundleId match
      3. substring fallback, regular activation policy only

    Helper processes have non-regular activation policy and are
    explicitly excluded from substring matches so multi-process apps
    like Chrome/Slack/Code resolve to their main UI process.
    """
    if not _AX_AVAILABLE:
        return None
    nm = (name or "").strip().lower()
    if not nm:
        return None
    apps = list(NSWorkspace.sharedWorkspace().runningApplications())
    # Pass 1: exact localized-name match (regular policy only)
    for a in apps:
        loc = (a.localizedName() or "").lower()
        if loc == nm and a.activationPolicy() == 0:
            return int(a.processIdentifier())
    # Pass 2: exact bundle-id match
    for a in apps:
        bid = (a.bundleIdentifier() or "").lower()
        if bid == nm:
            return int(a.processIdentifier())
    # Pass 3: substring on localized name, ONLY for regular-policy apps
    for a in apps:
        loc = (a.localizedName() or "").lower()
        if nm in loc and a.activationPolicy() == 0:
            return int(a.processIdentifier())
    # Pass 4: substring on bundleId (last resort)
    for a in apps:
        bid = (a.bundleIdentifier() or "").lower()
        if nm in bid and a.activationPolicy() == 0:
            return int(a.processIdentifier())
    return None


# When OpenSeer is running in daemon mode, daemon.py populates this
# set with every pid that should be treated as "the daemon's host
# terminal" — typically the terminal GUI app's pid PLUS its session
# helper(s) (e.g. iTermServer is parented by launchd, not by the GUI).
# We don't *block* AX of these pids — sometimes the user legitimately
# wants to drive iTerm — we just annotate the rendered AX block so
# the model knows it's looking at its own log window and can decide
# whether to act on it or pivot to `get_app_state app="<target>"`.
HOST_TERMINAL_PIDS: set[int] = set()


def active_app_pid(target_pid: int | None = None) -> int | None:
    """Return the system-frontmost app's pid, or None if unavailable.

    Uses Quartz's ``CGWindowListCopyWindowInfo`` (live z-order from the
    window server) rather than ``NSWorkspace.frontmostApplication()``.

    Why: NSWorkspace updates via NSDistributedNotificationCenter, which
    only fires when CFRunLoop is pumped. Our daemon (and the REPL too)
    runs as a CLI Python process with no Cocoa runloop, so NSWorkspace
    silently caches whatever app was frontmost at process startup
    (typically the host terminal). Even after the agent ``open_app``s
    Chrome and Chrome IS visually on top (proven by screenshots),
    NSWorkspace keeps reporting the terminal — so the AX dump targets
    the wrong app and the model is operating blind on web pages.

    Quartz returns the live, runloop-independent z-order, so this
    works correctly under daemon mode.

    Falls back to NSWorkspace if Quartz isn't available (non-Mac /
    sandboxed environments). ``target_pid`` is accepted for
    backwards compatibility and ignored.
    """
    if _CG_AVAILABLE:
        try:
            infos = CGWindowListCopyWindowInfo(
                kCGWindowListOptionOnScreenOnly, kCGNullWindowID,
            ) or []
        except Exception:
            infos = []
        for w in infos:
            try:
                # Layer 0 = normal app windows. Higher layers are menubar,
                # dock, status items, screensaver, etc — never our target.
                if int(w.get("kCGWindowLayer", 1)) != 0:
                    continue
                b = w.get("kCGWindowBounds") or {}
                # Skip tiny/offscreen windows so a 1×1 helper or a
                # collapsed window doesn't shadow the real frontmost.
                if (float(b.get("Width", 0)) < 100
                    or float(b.get("Height", 0)) < 100):
                    continue
                pid = int(w.get("kCGWindowOwnerPID", 0)) or None
                if pid:
                    return pid
            except Exception:
                continue
    if _AX_AVAILABLE:
        front = NSWorkspace.sharedWorkspace().frontmostApplication()
        if front is not None:
            return int(front.processIdentifier())
    return None


def _terminal_app_pids_in_ancestry() -> set[int]:
    """Return the set of pids in our parent chain that NSWorkspace /
    AX would also recognise as terminal-emulator GUI apps.

    Why a set rather than a single pid: iTerm's session lives in a
    helper process (iTermServer-3) that's *parented by launchd*, while
    the iTerm GUI app's pid is a sibling — we need both to flag AX
    dumps that hit either. We also include any running Cocoa app whose
    pid happens to be in our ancestry chain.
    """
    import os
    import subprocess

    chain: set[int] = set()
    pid = os.getppid()
    for _ in range(12):
        if pid <= 1:
            break
        chain.add(pid)
        try:
            r = subprocess.run(
                ["ps", "-o", "ppid=", "-p", str(pid)],
                capture_output=True, text=True, timeout=2,
            )
            ppid = int((r.stdout or "0").strip() or 0)
        except Exception:
            break
        if ppid == pid:
            break
        pid = ppid

    pids: set[int] = set(chain)
    # Cross-reference with NSWorkspace's running apps: iTerm's GUI pid
    # may not be in our parent chain (because iTermServer is parented by
    # launchd) but it'll be a running Cocoa app. Match by name family.
    if _AX_AVAILABLE:
        try:
            for app in NSWorkspace.sharedWorkspace().runningApplications():
                name = str(app.localizedName() or "").lower()
                if any(k in name for k in (
                    "iterm", "terminal", "warp", "tabby",
                    "alacritty", "ghostty", "kitty", "wezterm", "hyper",
                )):
                    pids.add(int(app.processIdentifier()))
        except Exception:
            pass
    return pids


def dump_ax_tree(pid: int | None = None,
                 *,
                 max_depth: int = 14,
                 max_elements: int = 150,
                 timeout: float = 5.0) -> list[AXElem]:
    """Walk the AX tree of an app and return interactive + static-text leaves.

    - `pid=None` defaults to the frontmost (excluding our own process,
      best-effort).
    - `max_depth` caps recursion; 14 is enough for most apps.
    - `max_elements` caps result list size; tree is walked depth-first
      so early elements (typically chrome / nav) win the budget.
    - `timeout` is the AX messaging timeout in seconds. Stock 2s often
      returns empty for Catalyst apps; 5s reliably populates.

    Returns [] when AX is unavailable, the pid is invalid, or the app
    refuses access.
    """
    if not _AX_AVAILABLE:
        return []
    if pid is None:
        pid = active_app_pid()
    if not pid:
        return []
    app = AXUIElementCreateApplication(pid)
    if app is None:
        return []
    try:
        AXUIElementSetMessagingTimeout(app, float(timeout))
    except Exception:
        pass

    out: list[AXElem] = []
    counter = [0]

    def visit(elem, depth: int) -> None:
        if len(out) >= max_elements or depth > max_depth:
            return
        role = _attr(elem, "AXRole") or ""
        title = _attr(elem, "AXTitle") or ""
        desc = _attr(elem, "AXDescription") or ""
        # AXValue can be many types; we only want strings as labels.
        value = _attr(elem, "AXValue")
        value_str = ""
        if isinstance(value, str):
            value_str = value
        label = title or desc or value_str

        pos = _unbox_pos(_attr(elem, "AXPosition"))
        size = _unbox_size(_attr(elem, "AXSize"))
        bbox = None
        if pos is not None and size is not None:
            x, y = pos
            w, h = size
            if w > 0 and h > 0:
                bbox = (int(x), int(y), int(w), int(h))

        is_interactive = role in _INTERACTIVE_ROLES
        is_static = role in _STATIC_ROLES
        # Keep the element if:
        #   - it's an interactive role (label optional — bbox is enough)
        #   - it's static text with a non-empty label
        # Skip pure containers / layout (AXGroup / AXScrollArea / ...) so
        # the prompt output stays readable.
        keep = False
        if is_interactive and bbox is not None:
            keep = True
        elif is_static and (label or "").strip() and bbox is not None:
            keep = True

        if keep:
            out.append(AXElem(
                idx=counter[0],
                role=str(role),
                label=str(label)[:100],
                bbox=bbox,
                depth=depth,
                interactive=is_interactive,
            ))
            counter[0] += 1

        children = _attr(elem, "AXChildren")
        if children:
            try:
                for c in children:
                    visit(c, depth + 1)
                    if len(out) >= max_elements:
                        return
            except TypeError:
                pass

    visit(app, 0)
    return out


def resolve_ax_index(elems: list[AXElem], query: str,
                      *, prefer_interactive: bool = True,
                      min_score: int = 30) -> tuple[int, int] | None:
    """Fuzzy-match ``query`` against the labels in a dumped AX tree.
    Returns ``(idx, score)`` for the best match, or ``None`` if no
    element scores above ``min_score``.

    Scoring (max 100):
      - exact label match (case-insensitive)           → 100
      - label starts with query                        → 80
      - query is a substring of label                  → 60
      - word-overlap ratio (set intersection / |q|)    → 0-50
      + interactive role bonus (button/link/etc.)      → +10
      + role match (e.g. query="button: submit")       → +15

    Why this exists: makes the agent's ``click(ax_query="Sign in")``
    work without forcing the model to count rows in the AX listing.
    Peekaboo's ``--on "Sign in"`` UX boiled down — same idea,
    different scoring weights.
    """
    if not query.strip():
        return None
    q = query.strip().lower()
    # Allow "role: label" syntax (e.g. "button: submit" / "link: docs")
    role_hint: str | None = None
    if ":" in q:
        head, tail = q.split(":", 1)
        head = head.strip()
        tail = tail.strip()
        if head and tail:
            role_hint = head
            q = tail
    best_idx: int | None = None
    best_score = -1
    for elem in elems:
        label = (elem.label or "").strip().lower()
        if not label:
            continue
        score = 0
        if label == q:
            score = 100
        elif label.startswith(q):
            score = 80
        elif q in label:
            score = 60
        else:
            qw = set(q.split())
            lw = set(label.split())
            if qw and lw:
                overlap = len(qw & lw) / len(qw)
                score = int(overlap * 50)
        if prefer_interactive and elem.interactive:
            score += 10
        if role_hint and role_hint in elem.role.lower():
            score += 15
        if score > best_score and score >= min_score:
            best_idx = elem.idx
            best_score = score
    if best_idx is None:
        return None
    return (best_idx, best_score)


def _resolve_to_live_element(pid: int, target_idx: int,
                              *, max_depth: int = 14,
                              timeout: float = 5.0):
    """Re-walk the AX tree of `pid` using the SAME visit/keep rules as
    `dump_ax_tree`, return the live ``AXUIElementRef`` whose flat
    index in that walk matches ``target_idx``.

    Used by ``set_ax_value`` and ``perform_ax_action`` so the
    agent's "act on element N" referencing a prior dump still works
    AX-side. Re-walking per action is the cost we pay until the
    snapshot-cache work (P1.1) lands and we can pin live refs
    inside a snapshot.

    Returns None on AX unavailable, bad pid, tree shorter than N+1,
    or on any AX exception (defensive — callers map None to a clean
    "element not found" error string for the agent).
    """
    if not _AX_AVAILABLE:
        return None
    app = AXUIElementCreateApplication(pid)
    if app is None:
        return None
    try:
        AXUIElementSetMessagingTimeout(app, float(timeout))
    except Exception:
        pass

    counter = [0]
    found = [None]

    def visit(elem, depth: int) -> None:
        if found[0] is not None or depth > max_depth:
            return
        role = _attr(elem, "AXRole") or ""
        title = _attr(elem, "AXTitle") or ""
        desc = _attr(elem, "AXDescription") or ""
        value = _attr(elem, "AXValue")
        value_str = value if isinstance(value, str) else ""
        label = title or desc or value_str

        pos = _unbox_pos(_attr(elem, "AXPosition"))
        size = _unbox_size(_attr(elem, "AXSize"))
        bbox = None
        if pos is not None and size is not None:
            x, y = pos
            w, h = size
            if w > 0 and h > 0:
                bbox = (int(x), int(y), int(w), int(h))

        is_interactive = role in _INTERACTIVE_ROLES
        is_static = role in _STATIC_ROLES
        keep = (
            (is_interactive and bbox is not None)
            or (is_static and (label or "").strip() and bbox is not None)
        )
        if keep:
            if counter[0] == target_idx:
                found[0] = elem
                return
            counter[0] += 1

        children = _attr(elem, "AXChildren")
        if children:
            try:
                for c in children:
                    visit(c, depth + 1)
                    if found[0] is not None:
                        return
            except TypeError:
                pass

    visit(app, 0)
    return found[0]


def set_ax_value(pid: int, idx: int, value: str,
                  *, timeout: float = 5.0) -> tuple[bool, str]:
    """Set the AXValue of the AX element at flat-index ``idx`` for
    process ``pid``. Works on text fields, sliders, switches, etc.
    — anything whose ``AXValue`` attribute is settable.

    Faster + more reliable than click+type chains: writes the
    value atomically without going through focus management,
    keyboard repeat, or autocomplete races. Sliders / checkboxes
    that can't accept keystrokes are this path's bread and butter.

    Returns ``(ok, error_message)``. The error message is short
    and meant for the agent's next-turn prompt.
    """
    if not _AX_AVAILABLE:
        return (False, "AX framework not available on this platform")
    elem = _resolve_to_live_element(pid, idx, timeout=timeout)
    if elem is None:
        return (False, f"no AX element at index {idx} for pid {pid} "
                       f"— tree may have shifted since last dump")
    # Verify settability first — AX will silently no-op on some
    # elements without returning an error, so this guard saves the
    # agent from "success → why didn't it change".
    try:
        err, settable = AXUIElementIsAttributeSettable(
            elem, "AXValue", None)
    except Exception as e:
        return (False, f"AXUIElementIsAttributeSettable raised: {e}")
    if err != 0:
        return (False, f"settability check failed (err={err})")
    if not settable:
        return (False,
                "element's AXValue is not settable — try `click` + "
                "`type` instead, or pick a different element")
    try:
        err = AXUIElementSetAttributeValue(elem, "AXValue", value)
    except Exception as e:
        return (False, f"AXUIElementSetAttributeValue raised: {e}")
    if err != 0:
        return (False, f"AX setValue returned err={err}")
    return (True, "")


def perform_ax_action(pid: int, idx: int, action: str = "AXPress",
                       *, timeout: float = 5.0) -> tuple[bool, str]:
    """Invoke a named AX action on the element at flat-index ``idx``.

    Common actions:
      - ``AXPress``      — primary activation (button / link / row)
      - ``AXShowMenu``   — open context menu (right-click semantic)
      - ``AXIncrement`` / ``AXDecrement`` — stepper / slider nudge
      - ``AXConfirm`` / ``AXCancel``      — dialog buttons
      - ``AXPick``       — popup-button selection

    These hit the same code path the OS dispatcher uses when YOU
    click, so they fire all the right notification / a11y events
    without going through synthetic mouse generation.

    Returns ``(ok, error_message)``.
    """
    if not _AX_AVAILABLE:
        return (False, "AX framework not available on this platform")
    elem = _resolve_to_live_element(pid, idx, timeout=timeout)
    if elem is None:
        return (False, f"no AX element at index {idx} for pid {pid} "
                       f"— tree may have shifted since last dump")
    # Enumerate supported actions so the error message can tell the
    # agent EXACTLY what is supported instead of just "err=...".
    try:
        err, names = AXUIElementCopyActionNames(elem, None)
    except Exception as e:
        return (False, f"AXUIElementCopyActionNames raised: {e}")
    if err != 0:
        return (False, f"could not list AX actions (err={err})")
    supported = list(names or [])
    if action not in supported:
        return (False,
                f"element doesn't support {action!r}. "
                f"Supported: {supported or '(none)'}")
    try:
        err = AXUIElementPerformAction(elem, action)
    except Exception as e:
        return (False, f"AXUIElementPerformAction raised: {e}")
    if err != 0:
        return (False, f"AX performAction({action}) returned err={err}")
    return (True, "")


def render_ax_for_prompt(elems: list[AXElem],
                         *,
                         max_lines: int = 80,
                         app_name: str | None = None,
                         pid: int | None = None) -> str:
    """Render an AX dump as a compact reference table for the prompt.

    Format (one line per element):
        idx=27  button   "人物传记,分组"   [781,240 106x193]

    Truncates at `max_lines` so a giant menu bar can't blow the prompt.

    If ``pid`` is in ``HOST_TERMINAL_PIDS`` (daemon mode), the header
    flags this as the daemon's own host terminal so the model can
    pivot via ``get_app_state(app="<target>")`` instead of acting on
    its own log output.
    """
    if not elems:
        return ""
    head = "## On-screen elements (accessibility tree)"
    if app_name:
        head += f" — {app_name}"
    if pid is not None and pid in HOST_TERMINAL_PIDS:
        head += (
            "\n[NOTE] This IS the terminal hosting OpenSeer's daemon — "
            "the [telegram]/[agent]/[step…] lines you may see in this "
            "tree are the daemon's own log output, not part of the "
            "task. Don't act on it unless the task explicitly involves "
            "this terminal. To work on a different app, call "
            '`get_app_state app="<name>"` to activate that app and dump '
            "its AX tree directly."
        )
    head += ("\nClick by index when present (more reliable than pixels). "
             "Static text rows are read-only.\n")

    lines: list[str] = [head]
    skipped = 0
    for e in elems:
        if len(lines) >= max_lines + 1:        # +1 for head
            skipped = len(elems) - (len(lines) - 1)
            break
        if e.bbox is None:
            continue
        x, y, w, h = e.bbox
        kind = e.role.replace("AX", "").lower()
        marker = "" if e.interactive else "  (static)"
        lab = e.label.replace("\n", " ")
        lines.append(f"  idx={e.idx:<3} {kind:<14} "
                     f"[{x},{y} {w}x{h}]  {lab!r}{marker}")
    if skipped:
        lines.append(f"  …({skipped} more elements omitted — prompt budget)")
    return "\n".join(lines) + "\n"
