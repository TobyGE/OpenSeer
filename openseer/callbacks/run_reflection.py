"""Post-run reflection and optional skill update proposal.

This callback runs after trajectory persistence, asks the configured model
for a short markdown reflection, appends it to trace.md, and optionally
applies a full SKILL.md replacement after user confirmation.
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

from .base import Callback
from ..skills import (
    canonical_skill_name,
    find_skill,
    find_skill_for_app,
    parse_skill_text,
    slugify_app,
    write_user_skill,
)


_SKILL_BLOCK_RE = re.compile(r"```skill-md\s*\n(.*?)\n```", re.DOTALL)
_APP_FROM_AX_RE = re.compile(r"accessibility tree\)\s+—\s+(.+)")
_URL_RE = re.compile(r"https?://([A-Za-z0-9.-]+)(?:[/:?#]|$)")
_MAX_RESULT = 500
_BROWSER_APPS = {"google chrome", "chrome", "safari", "firefox", "arc", "microsoft edge"}


REFLECTION_PROMPT = """You are OpenSeer's post-run reflection pass.

You receive a completed, failed, capped, or interrupted macOS agent run.
Do NOT continue the task and do NOT output action JSON.
Write a concise markdown reflection for the trace, and optionally propose a
durable skill update only when the run taught reusable app knowledge.

Rules:
- Use only evidence from the steps. Cite steps like [steps 4-9].
- Do not claim unverified success.
- Do not turn user-private task values into durable app facts. In both the
  reflection and skill body, replace contact names, group names, message text,
  search terms, URLs, and file names with generic placeholders unless they are
  app UI labels.
- If the failure was only network/API/SSL, do not propose a skill update for
  that cause.
- Prefer `Skill update: none` unless the run taught reusable app behavior.
- If an existing skill target is provided, update that exact skill name.
- Do not create task-specific skills such as wechat-message or wechat-group.
- One macOS app should usually have one CU skill named <app-slug>-mac.
- For browser-hosted websites, do not put site-specific facts into a generic
  browser skill such as google-chrome-mac. If a site target is provided, update
  or create that exact <site>-web skill and keep `requires.apps` set to the
  browser app used.
- If proposing a skill update, output one full merged SKILL.md body in a
  ```skill-md fenced block. Preserve existing verified facts and add only
  evidence-backed new facts.
- The skill body MUST have frontmatter with name, description, family, and
  requires.apps. For macOS UI skills use `family: cu`.

Output exactly this markdown shape:

## Run Reflection

Completion: complete|partial|incomplete

Lesson learned:
- ...

Skill update:
none

Or:

## Run Reflection

Completion: complete|partial|incomplete

Lesson learned:
- ...

Skill update:
update `skill-name`

```skill-md
<full merged SKILL.md>
```
"""


def _shorten(s: str | None, n: int = _MAX_RESULT) -> str:
    if not s:
        return ""
    s = " ".join(str(s).split())
    return s if len(s) <= n else s[: n - 1] + "…"


def extract_skill_block(markdown: str) -> str | None:
    m = _SKILL_BLOCK_RE.search(markdown or "")
    if not m:
        return None
    return m.group(1).strip() + "\n"


def _action_summary(action: Any) -> str:
    bits = [getattr(action, "name", "") or "<empty>"]
    if getattr(action, "app", None):
        bits.append(f"app={action.app!r}")
    if getattr(action, "skill_name", None):
        bits.append(f"skill_name={action.skill_name!r}")
    if getattr(action, "x", None) is not None:
        bits.append(f"xy=({action.x},{action.y})")
    if getattr(action, "index", None) is not None:
        bits.append(f"index={action.index}")
    if getattr(action, "text", None):
        bits.append(f"text={action.text!r}")
    if getattr(action, "key", None):
        bits.append(f"key={action.key}")
    if getattr(action, "amount", None) is not None:
        bits.append(f"amount={action.amount}")
    if getattr(action, "status", None):
        bits.append(f"status={action.status}")
    if getattr(action, "verified_by_steps", None):
        bits.append(f"verified_by_steps={action.verified_by_steps}")
    return " ".join(bits)


def _infer_app_name(history: list[Any], target_skill: Any | None) -> str:
    # Prefer explicit app fields from the agent's own actions.
    for s in reversed(history):
        app = getattr(s.action, "app", None)
        if app:
            return str(app)

    # Fall back to AX text returned by get_app_state.
    for s in reversed(history):
        m = _APP_FROM_AX_RE.search(s.result or "")
        if m:
            return m.group(1).strip()

    # If a matched skill has requires.apps, use its first app.
    if target_skill is not None:
        apps = (target_skill.requires or {}).get("apps") or []
        if isinstance(apps, list) and apps:
            return str(apps[0])
    return ""


def _read_skill_names(history: list[Any]) -> list[str]:
    out: list[str] = []
    for s in history:
        if getattr(s.action, "name", "") == "read_skill" and getattr(s.action, "skill_name", None):
            nm = str(s.action.skill_name)
            if nm not in out:
                out.append(nm)
    return out


def _domain_from_host(host: str) -> str:
    host = (host or "").strip().lower().lstrip(".")
    if host.startswith("www."):
        host = host[4:]
    return host


def _site_slug_from_domain(domain: str) -> str:
    parts = [p for p in _domain_from_host(domain).split(".") if p]
    if len(parts) >= 3 and parts[-2] in {"co", "com", "net", "org"}:
        parts = parts[:-2]
    elif len(parts) >= 2:
        parts = parts[:-1]
    return slugify_app(" ".join(parts) or domain)


def canonical_site_skill_name(domain: str) -> str:
    return f"{_site_slug_from_domain(domain)}-web"


def _infer_site_domain(history: list[Any], app_name: str) -> str:
    if (app_name or "").strip().lower() not in _BROWSER_APPS:
        return ""
    counts: dict[str, int] = {}
    for s in history:
        a = s.action
        fields = [
            getattr(a, "url", None),
            getattr(a, "text", None),
            getattr(a, "cmd", None),
            getattr(a, "thought", None),
            s.result or "",
        ]
        for value in fields:
            for m in _URL_RE.finditer(str(value or "")):
                domain = _domain_from_host(m.group(1))
                if not domain:
                    continue
                counts[domain] = counts.get(domain, 0) + 1
    if not counts:
        return ""
    return sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[0][0]


def _build_step_digest(history: list[Any]) -> str:
    lines: list[str] = []
    for s in history:
        a = s.action
        result = s.result or ""
        if getattr(a, "name", "") == "read_skill":
            result = f"read_skill {getattr(a, 'skill_name', '')} returned a skill body"
        lines.append(
            f"{s.idx}. thought={_shorten(getattr(a, 'thought', ''), 180)!r}; "
            f"action={_action_summary(a)}; result={_shorten(result)}"
        )
    return "\n".join(lines)


def _derive_final_status(out_dir: Path, history: list[Any]) -> dict:
    final_path = out_dir / "final.json"
    if final_path.exists():
        try:
            return json.loads(final_path.read_text(encoding="utf-8"))
        except Exception:
            pass
    last = history[-1] if history else None
    status = "empty"
    if last is not None:
        name = getattr(last.action, "name", "")
        if name == "terminate":
            status = (getattr(last.action, "status", None) or "done").lower()
        elif name in ("done", "fail", "verify_failed"):
            status = name
        else:
            status = "cap"
    return {"status": status, "error": None, "n_steps": len(history)}


class RunReflectionCallback(Callback):
    name = "RunReflection"

    def __init__(self, mode: str | None = None, verbose: bool = True) -> None:
        # ask: prompt before writing a durable skill
        # auto: write after validation
        # trace-only/off: append reflection but never write
        self.mode = (mode or os.environ.get("OPENSEER_SKILL_UPDATES") or "ask").strip().lower()
        self.verbose = verbose

    def on_run_end(self, ctx: dict[str, Any]) -> None:
        history = ctx.get("history") or []
        out_dir = Path(ctx["out_dir"])
        trace_path = out_dir / "trace.md"
        stream_full = ctx.get("stream_full")
        if not history or not callable(stream_full) or not trace_path.exists():
            return

        try:
            reflection = self._reflect(ctx, stream_full)
        except KeyboardInterrupt:
            if self.verbose:
                print("[reflection] interrupted; original run is already recorded")
            return
        except Exception as e:
            if self.verbose:
                print(f"[reflection] skipped: {e}")
            return

        try:
            with trace_path.open("a", encoding="utf-8") as f:
                f.write("\n\n")
                f.write(reflection.strip())
                f.write("\n")
        except Exception as e:
            if self.verbose:
                print(f"[reflection] could not append trace: {e}")
            return

        skill_body = extract_skill_block(reflection)
        if skill_body:
            self._maybe_apply_skill(ctx, skill_body, trace_path)

    def _reflect(self, ctx: dict[str, Any], stream_full) -> str:
        history = ctx.get("history") or []
        out_dir = Path(ctx["out_dir"])
        final = _derive_final_status(out_dir, history)
        skill_groups = ctx.get("skill_groups") or []
        read_names = _read_skill_names(history)
        target_skill = None
        app_name = _infer_app_name(history, None)
        site_domain = _infer_site_domain(history, app_name)
        site_skill = None
        if site_domain:
            site_skill = find_skill(skill_groups, canonical_site_skill_name(site_domain))

        if site_skill is not None:
            target_skill = site_skill
        elif app_name:
            target_skill = find_skill_for_app(skill_groups, app_name, preferred_names=read_names)
        elif read_names:
            target_skill = find_skill_for_app(skill_groups, "", preferred_names=read_names)
            app_name = _infer_app_name(history, target_skill)
            site_domain = _infer_site_domain(history, app_name)

        ui_actions = sum(
            1 for s in history
            if getattr(s.action, "name", "") in ("click", "type", "key", "scroll", "open_app")
        )
        existing_body = ""
        expected_skill_name = ""
        if target_skill is not None:
            expected_skill_name = target_skill.name
            if ui_actions >= 4 or read_names:
                existing_body = target_skill.path.read_text(encoding="utf-8")
        elif site_domain and ui_actions >= 4:
            expected_skill_name = canonical_site_skill_name(site_domain)
        elif app_name and ui_actions >= 4:
            expected_skill_name = canonical_skill_name(app_name)

        skill_target = "none"
        if expected_skill_name:
            target_kind = "website" if site_domain and expected_skill_name.endswith("-web") else "app"
            skill_target = (
                f"{'update existing' if target_skill else 'create new'} `{expected_skill_name}` "
                f"for {target_kind} {site_domain or app_name!r} using app {app_name!r}. "
                f"If you propose a skill, the frontmatter name MUST be exactly "
                f"{expected_skill_name!r}."
            )
        user_text = (
            f"TASK:\n{ctx.get('task', '')}\n\n"
            f"FINAL STATUS:\n{json.dumps(final, ensure_ascii=False)}\n\n"
            f"PRIMARY APP:\n{app_name or '(unknown)'}\n\n"
            f"PRIMARY WEBSITE:\n{site_domain or '(none)'}\n\n"
            f"SKILL TARGET:\n{skill_target}\n\n"
            f"STEPS:\n{_build_step_digest(history)}\n"
        )
        if existing_body:
            user_text += f"\nEXISTING SKILL BODY TO MERGE:\n{existing_body}\n"
        elif not expected_skill_name:
            user_text += "\nNo eligible skill target was found. Skill update must be none.\n"

        payload = {
            "model": ctx.get("model"),
            "instructions": REFLECTION_PROMPT,
            "input": [{"role": "user", "content": [
                {"type": "input_text", "text": user_text},
            ]}],
            "stream": True,
            "store": False,
            "reasoning": {"effort": "low"},
        }
        text, _events, _usage = stream_full(payload)
        return text.strip()

    def _maybe_apply_skill(self, ctx: dict[str, Any], skill_body: str, trace_path: Path) -> None:
        parsed = parse_skill_text(skill_body)
        if parsed is None:
            self._append_note(trace_path, "Skill apply skipped: proposed skill body has invalid frontmatter.")
            return

        expected = self._expected_skill_name(ctx, parsed.name)
        if expected and parsed.name != expected:
            self._append_note(
                trace_path,
                f"Skill apply skipped: proposed name `{parsed.name}` did not match expected `{expected}`.",
            )
            return

        if ctx.get("dry_run"):
            self._append_note(trace_path, "Skill apply skipped: run was dry-run.")
            return
        if self.mode in ("off", "trace-only", "none"):
            return

        if self.mode == "ask":
            print()
            print(f"  ◌ proposed skill update: {parsed.name}")
            print(f"    bytes: {len(skill_body)}")
            print("    --- BODY PREVIEW (first 40 lines) ---")
            for line in skill_body.splitlines()[:40]:
                print(f"    | {line}")
            if skill_body.count("\n") > 40:
                print(f"    | ... ({skill_body.count(chr(10)) - 40} more lines)")
            print("    --- END PREVIEW ---")
            try:
                ans = input("  Apply this skill update? [y/N] ").strip().lower()
            except EOFError:
                ans = ""
            if ans not in ("y", "yes"):
                self._append_note(trace_path, "Skill apply skipped: user did not confirm.")
                return

        res = write_user_skill(parsed.name, skill_body, dry_run=False)
        if res.ok:
            self._append_note(trace_path, f"Skill apply result: wrote `{parsed.name}` to {res.path}.")
            if self.verbose:
                print(f"  ✓ updated skill {parsed.name} → {res.path}")
        else:
            self._append_note(trace_path, f"Skill apply skipped: {res.error}")

    def _expected_skill_name(self, ctx: dict[str, Any], proposed_name: str) -> str:
        history = ctx.get("history") or []
        skill_groups = ctx.get("skill_groups") or []
        read_names = _read_skill_names(history)
        app_name = _infer_app_name(history, None)
        site_domain = _infer_site_domain(history, app_name)
        target_skill = None
        if site_domain:
            target_skill = find_skill(skill_groups, canonical_site_skill_name(site_domain))
        if target_skill is None and (app_name or read_names):
            target_skill = find_skill_for_app(skill_groups, app_name, preferred_names=read_names)
        if target_skill is not None:
            return target_skill.name
        if site_domain:
            return canonical_site_skill_name(site_domain)
        if app_name:
            return canonical_skill_name(app_name)
        return proposed_name

    def _append_note(self, trace_path: Path, note: str) -> None:
        try:
            with trace_path.open("a", encoding="utf-8") as f:
                f.write(f"\n_{note}_\n")
        except Exception:
            pass
