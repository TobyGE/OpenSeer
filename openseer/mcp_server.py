"""MCP (Model Context Protocol) server for OpenSeer.

Lets any MCP-compatible host (Codex CLI, Claude Code, Cursor) drive
macOS automation through OpenSeer's primitives — screenshot, click,
type, key, scroll, open_app, get_app_state — without running
OpenSeer's own agent loop. The host owns the reasoning; OpenSeer is
just the toolbox. (Following Peekaboo's MCP-server pattern.)

Run as: `openseer mcp serve`. Speaks JSON-RPC 2.0 over stdio per
MCP spec rev 2024-11-05. Newline-delimited JSON messages; no
Content-Length framing on stdio.

No external dependencies — written against pure stdlib because
the `mcp` PyPI package requires Python 3.10+ and we still support
3.9. Trade-off: we hand-roll only the parts of the protocol the
common hosts (Codex, Claude Code, Cursor) actually use.
"""
from __future__ import annotations

import base64
import io
import json
import logging
import sys
import traceback
from typing import Any, Callable


_PROTOCOL_VERSION = "2024-11-05"
_SERVER_NAME = "openseer"
_SERVER_VERSION = "0.1.5"


# Tool registry — each entry has metadata for the host's `tools/list`
# discovery plus a Python handler the dispatcher calls on
# `tools/call`. Handlers take an args dict and return a list of MCP
# content blocks ({"type": "text", "text": ...} or
# {"type": "image", "data": <b64 png>, "mimeType": "image/png"}).
_TOOLS: list[dict[str, Any]] = []


def _tool(name: str, description: str,
          input_schema: dict[str, Any]) -> Callable:
    def decorator(fn: Callable[[dict], list[dict]]):
        _TOOLS.append({
            "name": name,
            "description": description,
            "inputSchema": input_schema,
            "_handler": fn,
        })
        return fn
    return decorator


# ─── tools ──────────────────────────────────────────────────────────


@_tool(
    name="screenshot",
    description=(
        "Capture the current macOS screen and return as PNG. Use this "
        "to see what's currently on screen before deciding where to "
        "click or what's there."
    ),
    input_schema={"type": "object", "properties": {}},
)
def _tool_screenshot(args: dict) -> list[dict]:
    from .screen import capture
    frame = capture()
    buf = io.BytesIO()
    frame.image.save(buf, format="PNG")
    b64 = base64.b64encode(buf.getvalue()).decode("ascii")
    return [{
        "type": "image",
        "data": b64,
        "mimeType": "image/png",
    }]


@_tool(
    name="click",
    description=(
        "Click at screen coordinates. Use after `screenshot` or "
        "`get_app_state` to know where. x and y are pixel coordinates "
        "in the logical (point) resolution that screenshot uses."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "x":     {"type": "integer", "description": "X pixel"},
            "y":     {"type": "integer", "description": "Y pixel"},
            "count": {"type": "integer", "default": 1,
                      "description": "1=single, 2=double, 3=triple"},
        },
        "required": ["x", "y"],
    },
)
def _tool_click(args: dict) -> list[dict]:
    from .executor import Action, execute
    a = Action(name="click", x=int(args["x"]), y=int(args["y"]),
               count=int(args.get("count", 1)))
    return [{"type": "text", "text": execute(a, dry_run=False)}]


@_tool(
    name="type",
    description=(
        "Type text at the currently-focused field. If x,y given, "
        "click there first to establish focus."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "text": {"type": "string"},
            "x":    {"type": "integer"},
            "y":    {"type": "integer"},
        },
        "required": ["text"],
    },
)
def _tool_type(args: dict) -> list[dict]:
    from .executor import Action, execute
    x = args.get("x")
    y = args.get("y")
    a = Action(name="type", text=args["text"],
               x=int(x) if x is not None else None,
               y=int(y) if y is not None else None)
    return [{"type": "text", "text": execute(a, dry_run=False)}]


@_tool(
    name="key",
    description=(
        "Press a key or key combo. Examples: \"cmd+a\", \"enter\", "
        "\"esc\", \"pageup\", \"cmd+shift+t\". macOS modifiers: "
        "cmd, shift, option (or alt), ctrl."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "key": {"type": "string", "description": "Key combo"},
        },
        "required": ["key"],
    },
)
def _tool_key(args: dict) -> list[dict]:
    from .executor import Action, execute
    a = Action(name="key", key=args["key"])
    return [{"type": "text", "text": execute(a, dry_run=False)}]


@_tool(
    name="scroll",
    description=(
        "Scroll at coordinates. amount positive = down, negative = up. "
        "Magnitude: ~50 is a small nudge, ~150 is roughly a page."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "x":      {"type": "integer"},
            "y":      {"type": "integer"},
            "amount": {"type": "integer"},
        },
        "required": ["x", "y", "amount"],
    },
)
def _tool_scroll(args: dict) -> list[dict]:
    from .executor import Action, execute
    a = Action(name="scroll", x=int(args["x"]), y=int(args["y"]),
               amount=int(args["amount"]))
    return [{"type": "text", "text": execute(a, dry_run=False)}]


@_tool(
    name="open_app",
    description=(
        "Open or focus a macOS app by name. Examples: \"Calculator\", "
        "\"Safari\", \"Visual Studio Code\", \"WeChat\", \"微信\". Uses "
        "AppleScript activation; bypasses the Dock."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "app": {"type": "string"},
        },
        "required": ["app"],
    },
)
def _tool_open_app(args: dict) -> list[dict]:
    from .executor import Action, execute
    a = Action(name="open_app", app=args["app"])
    return [{"type": "text", "text": execute(a, dry_run=False)}]


@_tool(
    name="get_app_state",
    description=(
        "Dump the macOS Accessibility tree of the frontmost app (or "
        "a named one) as an indexed list of clickable / readable "
        "elements with their labels and screen-coord bboxes. Use "
        "this instead of guessing pixel coords from a screenshot — "
        "much more accurate. Returns text; pass `idx` to your next "
        "click via x,y from the matching element's center."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "app": {"type": "string",
                     "description": "App name; default = frontmost"},
        },
    },
)
def _tool_get_app_state(args: dict) -> list[dict]:
    from openseer_ax import (active_app_pid, app_pid_by_name, dump_ax_tree,
                             render_ax_for_prompt)
    name = args.get("app")
    pid = app_pid_by_name(name) if name else active_app_pid()
    if not pid:
        return [{"type": "text",
                  "text": f"app not found / not frontmost: {name or '(frontmost)'}"}]
    elems = dump_ax_tree(pid=pid)
    return [{"type": "text",
              "text": render_ax_for_prompt(elems, max_lines=200) or "(no elements)"}]


# ─── JSON-RPC dispatch ──────────────────────────────────────────────


def _send(msg: dict) -> None:
    sys.stdout.write(json.dumps(msg, ensure_ascii=False))
    sys.stdout.write("\n")
    sys.stdout.flush()


def _ok(req_id: Any, result: Any) -> None:
    _send({"jsonrpc": "2.0", "id": req_id, "result": result})


def _err(req_id: Any, code: int, message: str, data: Any = None) -> None:
    err: dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        err["data"] = data
    _send({"jsonrpc": "2.0", "id": req_id, "error": err})


def _handle(msg: dict, log: logging.Logger) -> None:
    method = msg.get("method")
    req_id = msg.get("id")
    params = msg.get("params") or {}

    if method == "initialize":
        _ok(req_id, {
            "protocolVersion": _PROTOCOL_VERSION,
            "capabilities": {"tools": {}},
            "serverInfo": {
                "name": _SERVER_NAME,
                "version": _SERVER_VERSION,
            },
        })
        return

    if method == "notifications/initialized":
        # Notification — no response, but we use it as a "ready"
        # signal in the log.
        log.info("client initialized")
        return

    if method == "tools/list":
        tools = [
            {k: v for k, v in t.items() if k != "_handler"}
            for t in _TOOLS
        ]
        _ok(req_id, {"tools": tools})
        return

    if method == "tools/call":
        name = params.get("name", "")
        args = params.get("arguments") or {}
        tool = next((t for t in _TOOLS if t["name"] == name), None)
        if tool is None:
            _err(req_id, -32602, f"unknown tool: {name}")
            return
        try:
            content = tool["_handler"](args)
            _ok(req_id, {"content": content, "isError": False})
        except Exception as e:
            tb = traceback.format_exc(limit=4)
            log.exception("tool %s crashed", name)
            # MCP convention: errors during a tool call are returned
            # as `isError: true` inside the result, NOT as a JSON-RPC
            # error — that way the host (Codex/Claude) sees the
            # error text as a tool response it can show the model.
            _ok(req_id, {
                "content": [{
                    "type": "text",
                    "text": f"tool '{name}' crashed: {e}\n\n{tb}",
                }],
                "isError": True,
            })
        return

    if method == "ping":
        # MCP liveness check — spec requires we just return success
        # with an empty result. Hosts like the MCP Inspector mark
        # the server unhealthy and stop using it if we return
        # method-not-found here.
        _ok(req_id, {})
        return

    # Stubs for protocol methods we don't implement yet; return
    # empty so the host doesn't error out asking for them.
    if method == "resources/list":
        _ok(req_id, {"resources": []})
        return
    if method == "prompts/list":
        _ok(req_id, {"prompts": []})
        return

    if req_id is not None:
        _err(req_id, -32601, f"method not found: {method}")


def run_mcp_server() -> int:
    """Read newline-delimited JSON-RPC messages from stdin, dispatch
    to tool handlers, write responses to stdout. Per MCP spec rev
    2024-11-05 stdio transport."""
    logging.basicConfig(
        level=logging.WARNING,
        format="%(asctime)s [mcp] %(message)s",
        stream=sys.stderr,
    )
    log = logging.getLogger("openseer.mcp")
    log.info("openseer mcp server starting (protocol %s, %d tools)",
             _PROTOCOL_VERSION, len(_TOOLS))
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError as e:
            log.warning("bad json line: %s", e)
            continue
        try:
            _handle(msg, log)
        except Exception as e:
            log.exception("dispatch crashed")
            req_id = msg.get("id")
            if req_id is not None:
                _err(req_id, -32603, f"server crashed: {e}")
    log.info("openseer mcp server exiting (stdin closed)")
    return 0
