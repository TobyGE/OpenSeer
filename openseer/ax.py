"""macOS Accessibility-tree wrapper.

Pulls the AX tree of the frontmost app (or a named one), filters down to
interactive elements with labels + bboxes, and exposes them as an indexed
list the agent can click by index instead of pixel coordinates.

This is the same primitive Codex CU's `get_app_state` exposes — element
indices are the difference between "guess where the button is" and
"click(index=27)" / 100% hit rate.

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
