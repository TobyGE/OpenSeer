"""Minimal computer-use agent: GPT-5.5 (via ChatGPT OAuth) drives macOS via pyautogui.

Single-step loop. Each turn:
  1. screencapture → logical-res PIL image
  2. send (system prompt, task, recent action history, current screenshot) to GPT-5.5
  3. parse one action JSON
  4. annotate screenshot with predicted action, save to ~/Desktop/openseer/run-{ts}/
  5. execute (or print, if dry_run)
  6. loop until done/fail/step cap
"""
from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from .callbacks import (
    BudgetCallback, Callback, ImageRetentionCallback, SafetyCallback,
    TrajectoryCallback,
)
from .draw import annotate
from .events import EventType, TaskEvent
from .executor import Action, execute
from .grounding import Grounder, GroundingResult, make as make_grounder
from .openai_chatgpt import MODEL as OAI_MODEL, _data_url, _stream_full
from .screen import Frame, capture
from .skills import load_available, render_for_prompt


SYSTEM_PROMPT = """You are OpenSeer, an autonomous macOS agent. You complete
the user's task by combining shell commands, web access, and direct GUI
control. Each turn you see the task, the prior steps you took, and the
current screen at {W}x{H} pixels.

## Tools

You output ONE JSON object per turn. No prose, no fences.

- `bash`              — run a shell command. `cmd`, optional `cwd`,
                        `timeout` (≤120s). Returns rc/stdout/stderr.
- `web_search`        — search the web. `query`, optional `amount` (count,
                        default 5), optional `freshness` ∈ day/week/month/year.
                        Returns ranked title + url + snippet.
- `web_fetch`         — fetch a URL and return its text. `url`.
- `click` / `scroll`  — `x`, `y` (logical pixels). `click` takes optional
                        `count` (2 = double, 3 = triple).
- `type`              — `text`, plus `x`, `y` of the target field
                        (executor clicks then types — atomic focus).
- `key`               — `key` like `"cmd+w"`, `"enter"`, `"esc"`, `"tab"`.
- `open_app`          — `app` = application name. Bypasses the Dock; use
                        this instead of clicking Dock icons.
- `wait`              — `amount` seconds (≤5). Use after launching apps.
- `reground`          — locate something visually. `target` (description),
                        optional `region` [x1,y1,x2,y2] crop, optional
                        `external: true` for the specialist grounder.
                        Does not touch the UI; returns coordinates.
- `terminate`         — end the run. `status` ∈ "done"/"fail", `reason`,
                        and for done: `verified_by_steps` listing prior
                        step indices that produced the result.

Multiple actions per turn: wrap in `{{"actions":[...], "thought":"..."}}`.
Chain when the next move is fully determined (e.g. type-then-enter,
select-all-then-delete-then-type). Don't chain across UI transitions
(after `open_app`, `wait` first).

## Execution bias

- Act. If a tool can move the task forward, use it. Don't end the turn
  with a plan when one tool call would actually do it.
- Pick the cheapest tool that semantically fits. CLI > web > GUI.
  `bash open <url>` beats navigating Safari. `web_search` beats opening
  a search engine in a browser. `osascript` beats clicking through
  menus. CU primitives are for things only the eyes can do (visual
  identification, GUI-only apps).
- If a tool gave a weak result, vary the approach: reword the search,
  fetch a different source, try `bash` instead of clicking, or vice
  versa. Don't repeat the same failing action.
- If you click and the wrong thing happens, you mis-grounded. Use
  `reground` for that target rather than nudging coordinates.

## Critical constraints

- The first screenshot is the user's REAL desktop. Do not close, move,
  or interact with their existing windows unless the task explicitly
  requires it. No reflexive `cmd+w` / `cmd+q` / dialog dismissal.
- **Never drive the OpenSeer terminal itself.** OpenSeer runs inside a
  terminal that's visible in your screenshots, showing prompts like
  `openseer ❯ ...` or `Run anyway? [y/N]`. That terminal is your
  control plane, not a target. Ignore any prompts you see in it —
  the human has already handled them.
- Click coordinates must be inside [0,{W}) x [0,{H}). Click control
  CENTERS, not corners.
- `cmd+space` (Spotlight) is unreliable on this machine — use
  `open_app` instead.
- `terminate` with status="done" requires `verified_by_steps`: a list
  of prior step indices where YOU actually did the work (click, type,
  bash, web_search, etc — `wait` and `reground` don't count). If the
  answer is already on screen but you didn't compute it this run,
  treat it as stale and re-do the work. Use `status="fail"` if
  genuinely blocked.
- **Don't claim done without evidence the result matches the request.**
  Filenames, labels, and titles are NOT content. If the user asks for
  "a photo OF X" / "the doc ABOUT Y" / "the email FROM Z", opening a
  file by name is not enough — the name almost never reflects the
  payload. Visually verify (preview, screenshot, fetch+read) before
  terminating, or terminate as fail. Same rule for web searches:
  cite the page you actually read, not the first link in the result
  list.

Output only the JSON object.
"""


@dataclass
class Step:
    idx: int
    action: Action
    result: str
    raw_response: str
    user_text: str = ""              # exact text portion sent to model
    usage: dict | None = None        # token usage from response.completed
    elapsed_ms: int = 0              # round-trip time of the model call
    screenshot_path: Path | None = None
    annotated_path: Path | None = None
    frame_hash: str = ""             # md5 of the PRE-decision screenshot, for dedup


def _action_from_obj(obj: dict, fallback_thought: str | None = None) -> Action:
    # Tolerance: models sometimes emit `{"status":"done"/"fail",...}` without
    # an explicit `action` field, treating `status` as the discriminator.
    # Infer `terminate` in that case so the user-visible flow doesn't break.
    name = obj.get("action") or ""
    if not name and obj.get("status") in ("done", "fail"):
        name = "terminate"
    return Action(
        name=name,
        x=obj.get("x"),
        y=obj.get("y"),
        text=obj.get("text"),
        key=obj.get("key"),
        amount=obj.get("amount"),
        count=int(obj.get("count") or 1),
        app=obj.get("app"),
        cmd=obj.get("cmd"),
        cwd=obj.get("cwd"),
        timeout=int(obj.get("timeout") or 30),
        query=obj.get("query"),
        url=obj.get("url"),
        freshness=obj.get("freshness"),
        target=obj.get("target"),
        region=obj.get("region"),
        external=bool(obj.get("external", False)),
        status=obj.get("status"),
        reason=obj.get("reason"),
        thought=obj.get("thought") or fallback_thought,
        verified_by_steps=obj.get("verified_by_steps"),
    )


def _parse_actions(raw: str) -> list[Action]:
    """Parse the model's response into one or more Actions.

    Accepts:
      {"action": "...", ...}              → single action
      {"actions": [{...}, {...}], "thought": "..."}  → chain
    """
    s = raw.strip()
    if s.startswith("```"):
        s = re.sub(r"^```(?:json)?\s*", "", s)
        s = re.sub(r"\s*```$", "", s)
    start = s.find("{")
    if start < 0:
        raise ValueError(f"no JSON object in: {raw!r}")
    depth = 0
    end = -1
    in_str = False
    esc = False
    for i in range(start, len(s)):
        ch = s[i]
        if in_str:
            if esc: esc = False
            elif ch == "\\": esc = True
            elif ch == '"': in_str = False
        else:
            if ch == '"': in_str = True
            elif ch == "{": depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    end = i + 1
                    break
    if end < 0:
        raise ValueError(f"unbalanced JSON in: {raw!r}")
    obj = json.loads(s[start:end])

    if "actions" in obj and isinstance(obj["actions"], list):
        chain_thought = obj.get("thought")
        return [_action_from_obj(a, fallback_thought=chain_thought)
                for a in obj["actions"]]
    return [_action_from_obj(obj)]


# Action types that count as the agent actively producing output / state
# changes. `open_app` is a state change too. `wait` / `reground` /
# `screenshot` do NOT count as evidence of having done the work.
_PRODUCING_ACTIONS = {"click", "double_click", "type", "key", "scroll",
                      "open_app", "bash", "web_search", "web_fetch"}


def _validate_done(action: Action, history: list["Step"]) -> str | None:
    """Returns None if `done` is acceptable, otherwise an error string
    explaining why so the model can be told to keep working.

    Accepts both legacy `done`/`fail` and the new `terminate` form.
    Only `terminate` with status="done" (or legacy `done`) needs verification —
    `fail` is the model giving up and doesn't need a verification chain.
    """
    # `terminate` defaults to status="done" (matches executor); treat
    # missing status the same as explicit "done" so the verification
    # chain isn't bypassable by simply omitting status.
    is_done = action.name == "done" or (
        action.name == "terminate" and (action.status or "done").lower() == "done"
    )
    if not is_done:
        return None
    cited = action.verified_by_steps or []
    if not cited:
        return ("`done` requires `verified_by_steps` listing prior step indices "
                "where you actively produced this result. You provided none. "
                "Either continue the task and produce the result yourself, "
                "or list the actual steps that produced it.")
    by_idx = {s.idx: s for s in history}
    invalid = [i for i in cited if i not in by_idx]
    if invalid:
        return f"verified_by_steps contains steps that don't exist: {invalid}"
    producing = [i for i in cited
                 if by_idx[i].action.name in _PRODUCING_ACTIONS]
    if not producing:
        cited_kinds = [(i, by_idx[i].action.name) for i in cited]
        return (f"None of the cited verification steps are producing actions. "
                f"Got: {cited_kinds}. You need at least one click/type/key/"
                f"scroll step that actually drove the result. If the answer "
                f"appeared without you doing the work, treat it as stale and "
                f"re-do the computation yourself.")
    return None


def _action_brief(a: Action) -> str:
    """Short description of an action for events / progress UI."""
    bits = [a.name]
    if a.x is not None:    bits.append(f"({a.x},{a.y})")
    if a.cmd:              bits.append(f"cmd={a.cmd[:40]!r}")
    if a.query:            bits.append(f"query={a.query[:40]!r}")
    if a.url:              bits.append(f"url={a.url[:60]}")
    if a.text:             bits.append(f"text={a.text[:30]!r}")
    if a.key:              bits.append(f"key={a.key}")
    if a.app:              bits.append(f"app={a.app!r}")
    if a.target:           bits.append(f"target={a.target[:30]!r}")
    if a.amount is not None: bits.append(f"amt={a.amount}")
    if a.status:           bits.append(f"status={a.status}")
    return " ".join(bits)


def _result_summary(s: "Step") -> str:
    """One-line description of what step `s` did and what changed afterwards."""
    a = s.action
    bits = [a.name]
    if a.x is not None:    bits.append(f"({a.x},{a.y})")
    if a.text:             bits.append(f"text={a.text!r}")
    if a.key:              bits.append(f"key={a.key}")
    if a.amount is not None: bits.append(f"amt={a.amount}")
    if a.verified_by_steps: bits.append(f"verified_by={a.verified_by_steps}")
    return f"step {s.idx} action: {' '.join(bits)}  result: {s.result}"


def _build_input(task: str, frame: Frame, current_hash: str,
                 history: list[Step],
                 session_context: str = "") -> list[dict]:
    """Construct a full multi-turn `input` array for the Responses API.

    Layout:
      user[0]:    task + disclaimer + INITIAL desktop screenshot
      assistant:  <step 1 raw JSON>
      user:       result of step 1 + screenshot AFTER step 1
                  (or "screen UNCHANGED" text if pixel-identical to the
                   image we sent in the immediately preceding user turn)
      ...

    The only token-saving trick built in here is the consecutive-frame
    dedup — pixel-identical to the previous image, drop the image and
    note "UNCHANGED" so the model knows its last action had no effect.

    Sliding-window image retention (keep N most recent) is NOT done here;
    `ImageRetentionCallback` runs after this and prunes older images.
    """
    items: list[dict] = []
    last_sent_hash: str = ""

    ctx_block = (session_context.rstrip() + "\n\n") if session_context else ""
    initial_text = (
        f"{ctx_block}TASK: {task}\n\n"
        "The screenshot below is the user's CURRENT desktop at the start. "
        "Treat their existing windows as read-only background — do NOT "
        "close, dismiss, or interrupt anything unless the task requires it.\n\n"
        "Output one action as JSON."
    )

    if not history:
        items.append({"role": "user", "content": [
            {"type": "input_image", "image_url": _data_url(frame.image)},
            {"type": "input_text",  "text": initial_text},
        ]})
        return items

    # initial user turn
    first = history[0]
    items.append({"role": "user", "content": [
        {"type": "input_image", "image_url": _data_url(_load_image(first.screenshot_path))},
        {"type": "input_text",  "text": initial_text},
    ]})
    last_sent_hash = first.frame_hash

    n = len(history)
    for i, s in enumerate(history):
        items.append({"role": "assistant", "content": [
            {"type": "output_text", "text": s.raw_response},
        ]})

        if i + 1 < n:
            after_path = history[i + 1].screenshot_path
            after_hash = history[i + 1].frame_hash
            after_img = None
        else:
            after_path = None
            after_hash = current_hash
            after_img = frame.image

        content: list[dict] = []
        if after_hash and after_hash == last_sent_hash:
            content.append({"type": "input_text", "text": (
                f"{_result_summary(s)}\n"
                "[screen UNCHANGED from previous turn — your last action "
                "had no visible effect]\n"
                + ("Output the next action as JSON." if i + 1 < n else "")
            )})
        else:
            if after_img is None:
                after_img = _load_image(after_path)
            content.append({"type": "input_image", "image_url": _data_url(after_img)})
            content.append({"type": "input_text", "text": (
                f"{_result_summary(s)}\n"
                "(screenshot after this step is shown above)\n"
                + ("Output the next action as JSON." if i + 1 < n else "")
            )})
            last_sent_hash = after_hash

        items.append({"role": "user", "content": content})

    return items


def _load_image(path):
    from PIL import Image
    return Image.open(path).convert("RGB")


def _hash_frame(img) -> str:
    """Stable pixel-content hash. Used to detect 'screen unchanged' across
    turns so we can skip resending identical images and explicitly tell the
    model its last action had no visible effect."""
    import hashlib
    return hashlib.md5(img.tobytes()).hexdigest()


def _ask_model(instructions: str, input_items: list[dict],
               reasoning_effort: str = "low",
               ) -> tuple[str, list[dict], dict]:
    """Send a pre-built multi-turn input to GPT-5.5. Returns (text, events, usage).

    `reasoning_effort` defaults to "low" for speed. Most planning steps are
    routine (open this, click that) and don't benefit from deep reasoning.
    Bump to "medium" / "high" only for genuinely hard cases.
    """
    payload = {
        "model": OAI_MODEL,
        "instructions": instructions,
        "input": input_items,
        "stream": True,
        "store": False,
        "reasoning": {"effort": reasoning_effort},
    }
    return _stream_full(payload)


def _confirm(action: Action) -> str:
    """Prompt the user before executing. Returns 'y' / 's' / 'q'."""
    summary = action.name
    if action.x is not None:    summary += f" ({action.x},{action.y})"
    if action.text:             summary += f" text={action.text!r}"
    if action.key:              summary += f" key={action.key}"
    if action.amount is not None: summary += f" amt={action.amount}"
    print(f"  >>> EXECUTE  {summary}  ?  [y]es / [s]kip / [q]uit ", end="", flush=True)
    try:
        ans = input().strip().lower()
    except EOFError:
        ans = "q"
    return ans[:1] if ans else "q"


def _default_callbacks(quiet: bool = False) -> list[Callback]:
    """Default callback stack: image retention (keep 4 most recent images,
    drop the rest with summary text) + per-step trajectory persistence +
    token budget + safety guard for dangerous bash/click."""
    return [
        ImageRetentionCallback(n=4, mode="summary"),
        TrajectoryCallback(verbose=not quiet),
        BudgetCallback(max_input_tokens=300_000, max_output_tokens=30_000,
                       verbose=not quiet),
        SafetyCallback(mode="confirm"),
    ]


# Where SKILL.md files live.
# - bundled: openseer/skills/  (ships with the wheel; package data)
# - user:    ~/.openseer/skills/  (override / extend per-machine, optional)
_BUNDLED_SKILLS_ROOT = Path(__file__).resolve().parent / "skills"
_USER_SKILLS_ROOT = Path.home() / ".openseer" / "skills"


def _skill_roots() -> list[Path]:
    # User skills first so they win the prompt budget over bundled ones
    # (the user-installed copy is the override; bundled is the fallback).
    return [p for p in (_USER_SKILLS_ROOT, _BUNDLED_SKILLS_ROOT) if p.exists()]


def _handle_reground(action: Action, frame: Frame,
                     default_grounder: Grounder,
                     external_grounder: Grounder) -> str:
    """Run a focused grounding pass on (a region of) the current frame.
    Returns a text result describing the resolved coordinates. Modifies
    `action` in place to record the answer (x,y, grounding_backend)."""
    if not action.target:
        return "ERROR: reground requires a `target` description"

    chosen = external_grounder if action.external else default_grounder

    img = frame.image
    region = action.region
    if region and len(region) == 4:
        x1, y1, x2, y2 = [int(v) for v in region]
        x1 = max(0, x1); y1 = max(0, y1)
        x2 = min(img.width, x2); y2 = min(img.height, y2)
        if x2 <= x1 or y2 <= y1:
            return f"ERROR: invalid region {region}"
        crop = img.crop((x1, y1, x2, y2))
        # We could upscale the crop here so the grounder sees more pixels
        # for tiny targets. For now keep native size — the model still
        # benefits from a tighter visual context.
        res = chosen.predict(crop, action.target)
        # map back to full-frame coords
        gx, gy = res.x + x1, res.y + y1
        action.x, action.y = gx, gy
        action.grounding_backend = chosen.name
        action.grounding_elapsed_ms = res.elapsed_ms
        scope = f"in region [{x1},{y1},{x2},{y2}]"
    else:
        res = chosen.predict(img, action.target)
        action.x, action.y = res.x, res.y
        action.grounding_backend = chosen.name
        action.grounding_elapsed_ms = res.elapsed_ms
        scope = "in full frame"

    return (
        f"reground[{chosen.name}{'/external' if action.external else ''}] "
        f"target={action.target!r} {scope} → ({action.x},{action.y}) "
        f"({res.elapsed_ms}ms)"
    )


def run(task: str, *, max_steps: int = 20, dry_run: bool = True,
        confirm_each: bool = False,
        out_dir: Path | None = None, sleep_between: float = 0.0,
        callbacks: list[Callback] | None = None,
        grounder: Grounder | str = "gpt55",
        external_grounder: Grounder | str | None = None,
        session_context: str = "",
        quiet: bool = False) -> list[Step]:
    """Run the agent loop. Returns the list of steps."""
    # Each task gets a short trace_id; runs land under ~/.openseer/runs/<id>/
    # so they're separate from user Desktop content. /show last and /history
    # both read this directory.
    import uuid as _uuid
    trace_id = _uuid.uuid4().hex[:8]
    if out_dir is None:
        out_dir = Path.home() / ".openseer" / "runs" / trace_id
    out_dir.mkdir(parents=True, exist_ok=True)

    cbs = callbacks if callbacks is not None else _default_callbacks(quiet=quiet)
    if isinstance(grounder, str):
        grounder = make_grounder(grounder)
    # Default external grounder = same as default. User overrides this on the
    # CLI to e.g. claude_cu / cup / ui_tars to actually get a SECOND opinion.
    if external_grounder is None:
        external_grounder = grounder
    elif isinstance(external_grounder, str):
        external_grounder = make_grounder(external_grounder)

    def say(*args, **kwargs):
        if not quiet:
            print(*args, **kwargs)

    say(f"[agent] task: {task}")
    say(f"[agent] dry_run={dry_run}  max_steps={max_steps}  out_dir={out_dir}")
    say(f"[agent] callbacks: {[c.label for c in cbs]}")
    say(f"[agent] grounder:  default={grounder.name}  external={external_grounder.name}")

    # Load skill knowledge once per run; injected into every system prompt.
    # We keep user and bundled groups SEPARATE through to render_for_prompt,
    # so the size cap evicts within the bundled tier first; user skills
    # always make it into the prompt as long as they fit at all.
    skill_groups: list[list] = []
    seen_names: set[str] = set()
    for root in _skill_roots():
        group: list = []
        for s in load_available(root):
            if s.name in seen_names:
                continue       # later root yields a shadowed bundled copy → skip
            seen_names.add(s.name)
            group.append(s)
        if group:
            skill_groups.append(group)
    skill_block = render_for_prompt(skill_groups)
    all_skills = [s for g in skill_groups for s in g]
    n_bash = sum(1 for s in all_skills if s.family == "bash")
    n_cu = sum(1 for s in all_skills if s.family == "cu")
    say(f"[agent] skills:    {len(all_skills)} loaded ({n_bash} bash, {n_cu} cu) "
        f"from {len(_skill_roots())} location(s)")

    history: list[Step] = []
    ctx: dict = {
        "task": task, "model": OAI_MODEL, "system_prompt": SYSTEM_PROMPT,
        "out_dir": out_dir, "max_steps": max_steps, "dry_run": dry_run,
        "history": history, "trace_id": trace_id,
        "started_at": time.time(),
        "session_context": session_context,    # prefix for first user msg, NOT stored as task
    }
    def emit(t: str, **data) -> None:
        """Broadcast a typed event to every callback subscribed via on_event."""
        ev = TaskEvent(type=t, step=ctx.get("step_idx"), data=data)
        for cb in cbs:
            cb.on_event(ctx, ev)

    def record_step(step) -> None:
        """Append a Step to history and fire BOTH the legacy on_step_recorded
        hook and the typed STEP_RECORDED event. Use this everywhere instead
        of bare append+on_step_recorded so progressive UIs see every step."""
        history.append(step)
        for cb in cbs:
            cb.on_step_recorded(ctx, step)
        emit(EventType.STEP_RECORDED, step_idx=step.idx,
             action=step.action.name, result=step.result)

    for cb in cbs:
        cb.on_run_start(ctx)
    emit(EventType.TASK_STARTED, task=task, model=OAI_MODEL,
         max_steps=max_steps, dry_run=dry_run)

    failed = False    # set when TASK_FAILED has been emitted (skip TASK_FINISHED)

    for i in range(max_steps):
        sn = i + 1
        ctx["step_idx"] = sn
        emit(EventType.STEP_STARTED)

        # budget / circuit-breakers can stop us before the next API call
        if not all(cb.on_should_continue(ctx) for cb in cbs):
            say(f"\n[agent] stopped by callback before step {sn}")
            break

        say(f"\n────── step {sn}/{max_steps} ──────")
        frame = capture()
        frame_hash = _hash_frame(frame.image)
        raw_path = out_dir / f"step{sn:02d}-raw.png"
        frame.image.save(raw_path)

        # build the multi-turn input, then let callbacks mutate it
        # (image retention dropping old screenshots happens here)
        instructions = SYSTEM_PROMPT.format(
            W=frame.logical_size[0], H=frame.logical_size[1])
        if skill_block:
            instructions = instructions + "\n\n" + skill_block
        input_items = _build_input(task, frame, frame_hash, history,
                                   session_context=session_context)
        for cb in cbs:
            input_items = cb.on_messages_built(ctx, input_items)

        emit(EventType.MODEL_STARTED, n_history=len(history))
        t0 = time.time()
        try:
            raw, events, usage = _ask_model(instructions, input_items)
        except Exception as e:
            say(f"  model error: {repr(e)[:200]}")
            (out_dir / f"step{sn:02d}-error.txt").write_text(repr(e))
            emit(EventType.TASK_FAILED, error=str(e))
            failed = True
            break
        elapsed_ms = int((time.time() - t0) * 1000)
        ctx["_last_events"] = events  # TrajectoryCallback reads this
        emit(EventType.MODEL_FINISHED, elapsed_ms=elapsed_ms, usage=usage,
             raw_chars=len(raw or ""))

        try:
            actions = _parse_actions(raw)
        except Exception as e:
            say(f"  parse error: {e}\n  raw: {raw[:300]!r}")
            emit(EventType.TASK_FAILED, error=f"parse error: {e}")
            failed = True
            break

        if len(actions) > 1:
            say(f"  CHAIN of {len(actions)} actions")
            emit(EventType.ACTION_PARSED, chain_len=len(actions))
        else:
            emit(EventType.ACTION_PARSED, chain_len=1)

        # Run each action in the chain. They share the same input frame /
        # raw screenshot, but each gets its own Step record and its own
        # annotated overlay. We re-screenshot only at the START of the
        # next outer iteration — the model sees one screen change for the
        # whole chain. This is the speedup of #4.
        terminate = False
        chain_aborted = False
        for chain_pos, action in enumerate(actions):
            sn_action = len(history) + 1
            label = f"step{sn_action:02d}"
            if len(actions) > 1:
                label = f"step{sn:02d}.{chain_pos+1}"

            # chain emits one thought for the whole sequence; print it only
            # once at the chain's first action so the console isn't flooded
            # with the same line repeated.
            if chain_pos == 0:
                tag = "chain-thought" if len(actions) > 1 else "thought"
                say(f"  [{label}] {tag}: {action.thought}")
            say(f"  [{label}] action:  {action.name}"
                  + (f" target={action.target!r}" if action.target else "")
                  + (f" ({action.x},{action.y})" if action.x is not None else "")
                  + (f" text={action.text!r}" if action.text else "")
                  + (f" key={action.key}"   if action.key  else "")
                  + (f" amount={action.amount}" if action.amount is not None else "")
                  + (f" reason={action.reason!r}" if action.reason else ""))

            # reground — runs grounding-only, doesn't touch UI
            if action.name == "reground":
                try:
                    result = _handle_reground(action, frame,
                                              grounder, external_grounder)
                except Exception as e:
                    result = f"ERROR: reground failed: {repr(e)[:200]}"
                marks = []
                if action.x is not None and action.y is not None:
                    marks.append((int(action.x), int(action.y),
                                  "openai_gpt-5.5",
                                  f"{label}: reground"))
                ann_path = out_dir / f"{label.replace('.', '_')}-action.png"
                (annotate(frame.image, marks) if marks else frame.image).save(ann_path)
                say(f"  [{label}] result:  {result}")
                step = Step(idx=sn_action, action=action, result=result,
                            raw_response=raw, usage=usage if chain_pos == 0 else None,
                            elapsed_ms=elapsed_ms if chain_pos == 0 else 0,
                            screenshot_path=raw_path, annotated_path=ann_path,
                            frame_hash=frame_hash)
                record_step(step)
                continue

            # annotate predicted action point on screenshot
            marks = []
            if action.x is not None and action.y is not None:
                marks.append((int(action.x), int(action.y),
                              "openai_gpt-5.5", f"{label}: {action.name}"))
            ann_path = out_dir / f"{label.replace('.', '_')}-action.png"
            (annotate(frame.image, marks) if marks else frame.image).save(ann_path)

            # per-step confirmation
            # Skip confirm prompt for terminal actions (done/fail/terminate) —
            # they end the loop, prompting "execute?" makes no sense and
            # treating Enter as abort would cancel a successful task.
            if confirm_each and not dry_run and action.name not in ("done", "fail", "terminate"):
                ans = _confirm(action)
                if ans == "q":
                    say("  [aborted by user]")
                    step = Step(idx=sn_action, action=action,
                                result="aborted by user before execution",
                                raw_response=raw, usage=usage if chain_pos == 0 else None,
                                elapsed_ms=elapsed_ms if chain_pos == 0 else 0,
                                screenshot_path=raw_path, annotated_path=ann_path,
                                frame_hash=frame_hash)
                    record_step(step)
                    chain_aborted = True
                    terminate = True
                    break
                if ans == "s":
                    step = Step(idx=sn_action, action=action,
                                result="skipped by user", raw_response=raw,
                                usage=usage if chain_pos == 0 else None,
                                elapsed_ms=elapsed_ms if chain_pos == 0 else 0,
                                screenshot_path=raw_path, annotated_path=ann_path,
                                frame_hash=frame_hash)
                    record_step(step)
                    continue

            # validate `done` (and `terminate` with status=done; missing
            # status defaults to done, matching executor behaviour)
            is_done = action.name == "done" or \
                (action.name == "terminate" and (action.status or "done").lower() == "done")
            if is_done:
                err = _validate_done(action, history)
                if err:
                    say(f"  [{label}] ⚠ done REJECTED: {err}")
                    rejected = Action(name="verify_failed",
                                      reason=err, thought=action.thought)
                    step = Step(idx=sn_action, action=rejected,
                                result=f"done rejected — {err}",
                                raw_response=raw,
                                usage=usage if chain_pos == 0 else None,
                                elapsed_ms=elapsed_ms if chain_pos == 0 else 0,
                                screenshot_path=raw_path, annotated_path=ann_path,
                                frame_hash=frame_hash)
                    record_step(step)
                    continue

            # Safety guard: ask SafetyCallback (if installed) whether this
            # action is suspicious. In dry_run we never actually execute,
            # so we just log the warning and let the preview proceed.
            safety_blocked = False
            for cb in cbs:
                if isinstance(cb, SafetyCallback):
                    ok, why = cb.check(action)
                    if not ok:
                        if dry_run:
                            say(f"  [{label}] ⚠ safety (dry-run): {why} — preview only, would prompt in real run")
                        elif cb.mode == "block":
                            say(f"  [{label}] ⚠ BLOCKED by safety: {why}")
                            safety_blocked = True
                        elif cb.mode == "confirm":
                            try:
                                ans = input(f"  ⚠ safety: {why}. Run anyway? [y/N] ").strip().lower()
                            except EOFError:
                                ans = ""
                            if ans not in ("y", "yes"):
                                say(f"  [{label}] aborted by safety check")
                                safety_blocked = True
                        else:  # "log" — just print
                            say(f"  [{label}] ⚠ safety log: {why} (running anyway)")
                    break
            if safety_blocked:
                # Safety rejection terminates the WHOLE run, not just this
                # action. Continuing risks a self-feedback loop: the next
                # screenshot still shows the safety prompt in the terminal
                # scrollback, and the model "helpfully" types `y` into it,
                # which drives the OpenSeer REPL itself.
                aborted = Action(name="terminate", status="fail",
                                 reason="aborted by safety guard",
                                 thought=action.thought)
                step = Step(idx=sn_action, action=aborted,
                            result="aborted by safety guard — run terminated",
                            raw_response=raw,
                            usage=usage if chain_pos == 0 else None,
                            elapsed_ms=elapsed_ms if chain_pos == 0 else 0,
                            screenshot_path=raw_path, annotated_path=ann_path,
                            frame_hash=frame_hash)
                record_step(step)
                say(f"\n[agent] aborted by safety guard — run terminated.")
                emit(EventType.SAFETY_BLOCKED,
                     name=action.name, reason="aborted by safety guard")
                terminate = True
                break

            emit(EventType.ACTION_STARTED, name=action.name,
                 chain_pos=chain_pos, chain_len=len(actions),
                 summary=_action_brief(action))
            result = execute(action, dry_run=dry_run)
            say(f"  [{label}] result:  {result}{'  [DRY-RUN]' if dry_run else ''}")
            emit(EventType.ACTION_FINISHED, name=action.name,
                 chain_pos=chain_pos, result=result, dry_run=dry_run)

            step = Step(idx=sn_action, action=action, result=result,
                        raw_response=raw,
                        usage=usage if chain_pos == 0 else None,
                        elapsed_ms=elapsed_ms if chain_pos == 0 else 0,
                        screenshot_path=raw_path, annotated_path=ann_path,
                        frame_hash=frame_hash)
            record_step(step)

            if action.name in ("done", "fail", "terminate"):
                lbl = action.status if action.name == "terminate" else action.name
                say(f"\n[agent] terminated: {lbl} — {action.reason}")
                terminate = True
                break

            # tiny pause between chained actions so UI catches up before
            # the next click in the chain (no full screenshot though)
            if len(actions) > 1 and chain_pos + 1 < len(actions):
                time.sleep(0.2)

        if terminate:
            break
        if sleep_between: time.sleep(sleep_between)

    # Final status: derive from the last action recorded. Skipped if a
    # TASK_FAILED was already emitted (in which case the consumer treats
    # that as the terminal event, and adding TASK_FINISHED here would be
    # a contradictory second terminal event).
    if not failed:
        last = history[-1] if history else None
        if last is None:
            final_status = "empty"
        elif last.action.name == "terminate":
            final_status = (last.action.status or "done").lower()
        elif last.action.name in ("done", "fail", "verify_failed"):
            final_status = last.action.name
        else:
            final_status = "cap"
        emit(EventType.TASK_FINISHED, status=final_status,
             n_steps=len(history))

    for cb in cbs:
        cb.on_run_end(ctx)

    return history
