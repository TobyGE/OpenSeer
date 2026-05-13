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
# Extract the bulleted lesson-learned summary from the reflection
# markdown so the GUI chip can show the user-facing reason without
# having to ship them the full reflection text.
_LESSON_BLOCK_RE = re.compile(
    r"Lesson learned:\s*\n(.*?)(?:\n\s*Skill update:|\Z)",
    re.DOTALL | re.IGNORECASE,
)
_APP_FROM_AX_RE = re.compile(r"accessibility tree\)\s+—\s+(.+)")
_OPENED_APP_RE = re.compile(r"opened app ['\"](.+?)['\"]")
_URL_RE = re.compile(r"https?://([A-Za-z0-9.-]+)(?:[/:?#]|$)")
_MAX_RESULT = 500
_BROWSER_APPS = {"google chrome", "chrome", "safari", "firefox", "arc", "microsoft edge"}
_SITE_ALIASES = (
    (re.compile(r"\bcostco\s+same[- ]day\b", re.IGNORECASE), "sameday.costco.com"),
    (re.compile(r"\binstacart\b", re.IGNORECASE), "instacart.com"),
)
_IGNORED_INFERENCE_DOMAINS = {"support.google.com"}


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
- Triggers for proposing a skill update — propose when ANY of these hold,
  not only when something went wrong:
    (a) the run completed (or partially completed) an end-to-end flow on
        a site/app whose multi-step happy-path order would save the next
        run from re-discovering it (e.g. AMC: result → Get Tickets →
        showtime → seat map → Continue → ticket type → Continue →
        food → Continue → Confirm Purchase). Worked-as-intended sequences
        are still durable knowledge.
    (b) the run discovered a footgun: a wrong URL route, a search
        submission gotcha, an overlay/suggestion requirement, a
        product-card navigation workaround, an invisible login wall,
        a stale tab handler, etc.
    (c) the run learned where a specific control lives (which sidebar
        houses purchase history, which keyboard shortcut opens search,
        which menu has the export option) when that location is not
        obvious from the page chrome alone.
  Do not leave it as `none` when the same lesson would clearly save
  future steps. End-to-end flow ordering on consumer sites is almost
  always trigger (a).
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


def extract_lesson_block(markdown: str) -> str:
    """Pull just the bulleted body under `Lesson learned:`.

    Returned text drops the section header but keeps the `- ...`
    bullets verbatim. Empty string when the model omitted the
    section or produced something unparseable.
    """
    m = _LESSON_BLOCK_RE.search(markdown or "")
    if not m:
        return ""
    return m.group(1).strip()


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

    # `open_app` results are stored as text in the trajectory; older
    # transcript rows did not persist `action.app`, so recover it here.
    for s in reversed(history):
        m = _OPENED_APP_RE.search(s.result or "")
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
    # Keep the full normalized domain in the skill name so the same site
    # always maps to one durable skill. We cannot use dots because skill
    # ids are validated as [a-z0-9_-], so dots become hyphens:
    # sameday.costco.com -> sameday-costco-com-web.
    return slugify_app(_domain_from_host(domain))


def canonical_site_skill_name(domain: str) -> str:
    return f"{_site_slug_from_domain(domain)}-web"


def _infer_site_domain(history: list[Any], app_name: str,
                       task_text: str = "",
                       visited_urls: list[str] | None = None) -> str:
    # Site detection runs for two task shapes:
    #   (a) browser-driven flows — app_name is Chrome / Safari / etc.
    #       and URLs come from `_browser_current_url()`.
    #   (b) bash / web_fetch / read_page flows — app_name is empty
    #       because the task never opened a native app, but URLs
    #       still show up in `action.cmd` / `action.url` / step
    #       results. These earn site skills too (arxiv-org-web,
    #       api-github-com-web, …).
    # A non-empty app_name that ISN'T a browser still bails — that's
    # a native-app task (WeChat, Calculator, …) and the durable
    # knowledge belongs on the app skill, not on whatever domain
    # showed up incidentally.
    app_lc = (app_name or "").strip().lower()
    if app_lc and app_lc not in _BROWSER_APPS:
        return ""
    for pat, domain in _SITE_ALIASES:
        if pat.search(task_text or ""):
            return domain
    text_blobs = [task_text or ""]
    counts: dict[str, int] = {}

    # Primary signal: URLs the agent loop actually probed via
    # _browser_current_url() each turn. Step.user_text (which would
    # contain the page-content block's URL line) isn't persisted on
    # Step objects, and clicks don't carry their target URL on
    # themselves, so without this list inference is effectively
    # blind on most browser tasks.
    for url in visited_urls or []:
        for m in _URL_RE.finditer(str(url or "")):
            domain = _domain_from_host(m.group(1))
            if not domain or domain in _IGNORED_INFERENCE_DOMAINS:
                continue
            counts[domain] = counts.get(domain, 0) + 1

    for s in history:
        a = s.action
        fields = [
            getattr(a, "url", None),
            getattr(a, "text", None),
            getattr(a, "cmd", None),
            getattr(a, "thought", None),
            s.result or "",
            getattr(s, "user_text", "") or "",
        ]
        for value in fields:
            text_blobs.append(str(value or ""))
            for m in _URL_RE.finditer(str(value or "")):
                domain = _domain_from_host(m.group(1))
                if not domain or domain in _IGNORED_INFERENCE_DOMAINS:
                    continue
                counts[domain] = counts.get(domain, 0) + 1
    if not counts:
        combined = "\n".join(text_blobs)
        for pat, domain in _SITE_ALIASES:
            if pat.search(combined):
                return domain
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
            # Persist the expected skill name as a sidecar so the
            # daemon's Telegram "Apply" button (which fires after the
            # run finishes and the in-memory ctx is gone) can re-run
            # the same expected-name check this callback applies in
            # _maybe_apply_skill. Without it, a click on Apply would
            # bypass the name guard and could write a skill the
            # in-process check already determined was wrong.
            try:
                expected = self._expected_skill_name(ctx, "")
                if expected:
                    (out_dir / "expected_skill.txt").write_text(
                        expected, encoding="utf-8",
                    )
            except Exception as e:
                if self.verbose:
                    print(f"[reflection] could not write "
                          f"expected_skill.txt: {e}")
            # Stash the proposed skill body alongside the run so the
            # voice orb's deferred apply_skill request can read it
            # back after the agent loop has exited. The model already
            # vetted the content during reflection — the GUI never
            # needs to (and shouldn't) reproduce it.
            try:
                (out_dir / "proposed_skill.md").write_text(
                    skill_body, encoding="utf-8",
                )
            except Exception as e:
                if self.verbose:
                    print(f"[reflection] could not write "
                          f"proposed_skill.md: {e}")
            lesson_text = extract_lesson_block(reflection)
            self._maybe_apply_skill(ctx, skill_body, trace_path,
                                    lesson_text=lesson_text)

    def _reflect(self, ctx: dict[str, Any], stream_full) -> str:
        history = ctx.get("history") or []
        out_dir = Path(ctx["out_dir"])
        final = _derive_final_status(out_dir, history)
        skill_groups = ctx.get("skill_groups") or []
        read_names = _read_skill_names(history)
        target_skill = None
        app_name = _infer_app_name(history, None)
        visited_urls = ctx.get("_browser_urls_visited") or []
        site_domain = _infer_site_domain(history, app_name,
                                         str(ctx.get("task", "")),
                                         visited_urls=visited_urls)
        site_skill = None
        if site_domain:
            site_skill = find_skill(skill_groups, canonical_site_skill_name(site_domain))

        if site_skill is not None:
            target_skill = site_skill
        elif site_domain:
            # We identified a website but no skill for it exists yet.
            # Don't fall through to find_skill_for_app(app=browser) —
            # that would match other site skills (e.g. x-com-web which
            # declares requires.apps: [Google Chrome]) and the model
            # would dutifully merge this run's site facts into the
            # wrong site's skill. Leave target_skill=None so the
            # reflection prompts a NEW site skill instead.
            target_skill = None
        elif app_name:
            target_skill = find_skill_for_app(skill_groups, app_name, preferred_names=read_names)
        elif read_names:
            target_skill = find_skill_for_app(skill_groups, "", preferred_names=read_names)
            app_name = _infer_app_name(history, target_skill)
            site_domain = _infer_site_domain(history, app_name,
                                             str(ctx.get("task", "")),
                                             visited_urls=visited_urls)

        ui_actions = sum(
            1 for s in history
            if getattr(s.action, "name", "") in ("click", "type", "key", "scroll", "open_app")
        )
        # Web-touching actions whose cmd / url / result mentions the
        # inferred site count as "substantive engagement". Without
        # this, API-scraping / page-reading flows (curl + python on
        # youtube.com, read_page on arxiv.org, …) have ui_actions=0
        # and never propose a skill — even when the run discovered a
        # durable footgun like "the YouTube timedtext caption endpoint
        # returns empty bytes; scrape shortDescription instead" or
        # "bs4 isn't installed; use stdlib parsing on arXiv HTML".
        # `read_page` matters here: agentd's HTML-fetch primitive
        # doesn't ride through `bash` so it'd be excluded otherwise.
        domain_bash_actions = 0
        if site_domain:
            for s in history:
                name = getattr(s.action, "name", "")
                if name not in ("bash", "web_fetch", "web_search",
                                 "read_page"):
                    continue
                blob = " ".join([
                    str(getattr(s.action, "cmd", "") or ""),
                    str(getattr(s.action, "url", "") or ""),
                    str(getattr(s.action, "query", "") or ""),
                    str(s.result or "")[:2000],
                ])
                if site_domain in blob.lower():
                    domain_bash_actions += 1
        substantive_actions = ui_actions + domain_bash_actions
        existing_body = ""
        expected_skill_name = ""
        if target_skill is not None:
            expected_skill_name = target_skill.name
            if substantive_actions >= 4 or read_names:
                existing_body = target_skill.path.read_text(encoding="utf-8")
        elif site_domain and substantive_actions >= 4:
            expected_skill_name = canonical_site_skill_name(site_domain)
        elif app_name and ui_actions >= 4:
            # No bash boost for app-only skills: those still need real UI
            # evidence (clicks/types) since the durable knowledge for a
            # native macOS app lives in its UI tree, not in a curl recipe.
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

    def _maybe_apply_skill(self, ctx: dict[str, Any], skill_body: str,
                           trace_path: Path,
                           lesson_text: str = "") -> None:
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
            # GUI / voice-orb path: fire SKILL_PROPOSED and let the
            # user click Save / Discard in the orb. The agentd WS
            # handler picks up `apply_skill {run_id}` later, reads
            # proposed_skill.md from disk, and writes via
            # write_user_skill. We don't block here — the agent
            # loop's worker thread needs to return so the daemon can
            # serve the apply request on the same loop.
            emit_event = ctx.get("_emit_event")
            if callable(emit_event):
                run_id = Path(ctx["out_dir"]).name
                emit_event(
                    "skill_proposed",
                    run_id=run_id,
                    skill_name=parsed.name,
                    is_new=(self._find_existing_skill(ctx, parsed.name) is None),
                    lesson=lesson_text,
                    body=skill_body,
                    bytes_=len(skill_body),
                )
                self._append_note(
                    trace_path,
                    f"Skill apply deferred: emitted SKILL_PROPOSED for "
                    f"`{parsed.name}` ({len(skill_body)} bytes); waiting "
                    f"for user.",
                )
                return
            # CLI fallback: print preview + input() prompt.
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

    def _find_existing_skill(self, ctx: dict[str, Any], name: str) -> Any:
        """Look up `name` in ctx's loaded skill groups; None if not found."""
        skill_groups = ctx.get("skill_groups") or []
        return find_skill(skill_groups, name)

    def _expected_skill_name(self, ctx: dict[str, Any], proposed_name: str) -> str:
        history = ctx.get("history") or []
        skill_groups = ctx.get("skill_groups") or []
        read_names = _read_skill_names(history)
        app_name = _infer_app_name(history, None)
        site_domain = _infer_site_domain(
            history, app_name, str(ctx.get("task", "")),
            visited_urls=ctx.get("_browser_urls_visited") or [],
        )
        target_skill = None
        if site_domain:
            target_skill = find_skill(skill_groups, canonical_site_skill_name(site_domain))
        # Mirror the no-fallback rule from _reflect: when a browser run
        # detected a site domain but no skill for that site exists yet,
        # don't fall back to find_skill_for_app(<browser>) — that would
        # match other site skills declaring the same browser as host
        # and the proposed new <site>-web name would get rejected as a
        # mismatch. Stay on the new-site path.
        if (target_skill is None and not site_domain
                and (app_name or read_names)):
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
