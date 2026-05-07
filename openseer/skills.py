"""Load Markdown skill files and inject them into the system prompt.

A skill is a folder under ``skills/`` containing one ``SKILL.md`` whose
top of the file is YAML front-matter:

    ---
    name: macos-calculator
    description: Drive macOS Calculator.app via keyboard shortcuts.
    family: cu                    # 'bash' or 'cu' or 'mixed'
    requires:
      bins: ['open']              # optional — gate availability
      apps: ['Calculator']        # optional — macOS .app names
    ---

    # Calculator skill body — markdown with playbooks.

The loader:
  1. scans ``skills/<family>/<name>/SKILL.md`` (and bare ``skills/*/SKILL.md``)
  2. checks `requires` against the current machine
  3. emits a single concatenated markdown block ready for the system prompt

Format choice: identical to OpenClaw's SKILL.md so future tooling (and
any cross-pollination of skill content) stays compatible.
"""
from __future__ import annotations

import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass
class Skill:
    name: str
    description: str
    family: str          # "bash" | "cu" | "mixed"
    body: str            # markdown body (no front-matter)
    requires: dict       # parsed `requires:` block
    path: Path           # path to SKILL.md


USER_SKILLS_ROOT = Path.home() / ".openseer" / "skills"
_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
_FRONTMATTER_RE = re.compile(
    r"\A---\s*\n(.*?)\n---\s*\n(.*)\Z", re.DOTALL,
)

_APP_ALIASES = {
    "微信": "WeChat",
    "weixin": "WeChat",
    "wechat": "WeChat",
    "微信读书": "WeRead",
}


def _parse_yaml_lite(text: str) -> dict[str, Any]:
    """Tiny YAML subset: top-level scalars, simple lists, single-level nests.

    Avoids pulling in PyYAML for a one-purpose tool. If a future skill
    needs richer YAML, swap to PyYAML later.
    """
    out: dict[str, Any] = {}
    cur_key: str | None = None
    cur_indent = -1
    current_dict: dict | None = None
    # When a key is opened with no scalar value, the next indented lines
    # may be either nested `key: value` pairs OR a block list of `- item`.
    # We accumulate block-list items into this list and attach back to the
    # owning key when the indent drops or another key starts.
    pending_list_owner: tuple[dict, str] | None = None
    pending_list_items: list[str] = []

    def _flush_pending_list() -> None:
        nonlocal pending_list_owner, pending_list_items
        if pending_list_owner is not None:
            owner, key = pending_list_owner
            owner[key] = list(pending_list_items)
        pending_list_owner = None
        pending_list_items = []

    for raw in text.splitlines():
        line = raw.rstrip()
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        indent = len(line) - len(line.lstrip())
        s = line.strip()
        # block-list item under a previously opened key (e.g. `apps:` then
        # `  - WeChat`). The leading `- ` is the YAML list marker.
        if s.startswith("- ") and pending_list_owner is not None:
            pending_list_items.append(_strip_quotes(s[2:].strip()))
            continue
        # nested dict mode
        if current_dict is not None and indent > cur_indent:
            if ":" in s:
                _flush_pending_list()
                k, _, v = s.partition(":")
                k = k.strip()
                v = v.strip()
                if v == "":
                    # could be nested dict OR block list — defer decision
                    current_dict[k] = {}
                    pending_list_owner = (current_dict, k)
                elif v.startswith("[") and v.endswith("]"):
                    current_dict[k] = _parse_inline_list(v)
                else:
                    current_dict[k] = _strip_quotes(v)
            continue
        # back to top
        _flush_pending_list()
        current_dict = None
        if ":" in s:
            k, _, v = s.partition(":")
            k = k.strip()
            v = v.strip()
            if v == "":
                # nested dict starts on subsequent indented lines
                out[k] = {}
                current_dict = out[k]
                cur_indent = indent
                cur_key = k
            elif v.startswith("[") and v.endswith("]"):
                out[k] = _parse_inline_list(v)
            else:
                out[k] = _strip_quotes(v)
    _flush_pending_list()
    return out


def _parse_inline_list(v: str) -> list[str]:
    inside = v.strip()[1:-1]
    if not inside.strip():
        return []
    return [_strip_quotes(p.strip()) for p in inside.split(",")]


def _strip_quotes(v: str) -> str:
    if len(v) >= 2 and v[0] == v[-1] and v[0] in ("'", '"'):
        return v[1:-1]
    return v


def parse_skill_text(text: str, path: Path | None = None) -> Skill | None:
    m = _FRONTMATTER_RE.match(text)
    if not m:
        return None
    front = _parse_yaml_lite(m.group(1))
    body = m.group(2).strip()
    path = path or Path("<memory>")
    name = str(front.get("name", path.parent.name))
    return Skill(
        name=name,
        description=str(front.get("description", "")),
        family=str(front.get("family", "")),
        body=body,
        requires=front.get("requires", {}) if isinstance(front.get("requires"), dict) else {},
        path=path,
    )


def parse_skill(path: Path) -> Skill | None:
    text = path.read_text(encoding="utf-8")
    return parse_skill_text(text, path)


@dataclass
class SkillWriteResult:
    ok: bool
    skill: Skill | None = None
    path: Path | None = None
    error: str | None = None
    dry_run: bool = False


def validate_skill_body(skill_name: str, body: str) -> SkillWriteResult:
    """Validate a full SKILL.md body before it becomes durable memory."""
    nm = (skill_name or "").strip()
    if not nm or not (body or "").strip():
        return SkillWriteResult(False, error="write_skill needs `skill_name` and `skill_body`")
    if not _ID_RE.match(nm):
        return SkillWriteResult(False, error=(
            f"skill_name {nm!r} must match [a-z0-9][a-z0-9_-]{{0,63}}"
        ))
    parsed = parse_skill_text(body)
    if parsed is None:
        return SkillWriteResult(False, error=(
            "skill body has no valid frontmatter. Must start with "
            "`---\\n...---\\n` containing name/description/family."
        ))
    if parsed.name != nm:
        return SkillWriteResult(False, error=(
            f"frontmatter name {parsed.name!r} doesn't match skill_name {nm!r}"
        ))
    family = parsed.family or "cu"
    if not _ID_RE.match(family):
        return SkillWriteResult(False, error=(
            f"family {family!r} must match [a-z0-9][a-z0-9_-]{{0,63}}"
        ))
    return SkillWriteResult(True, skill=parsed)


def write_user_skill(skill_name: str, body: str, *, dry_run: bool = False) -> SkillWriteResult:
    """Validate and write a user skill under ~/.openseer/skills."""
    res = validate_skill_body(skill_name, body)
    if not res.ok or res.skill is None:
        return res
    family = res.skill.family or "cu"
    dest = USER_SKILLS_ROOT / family / res.skill.name / "SKILL.md"
    if dry_run:
        return SkillWriteResult(True, skill=res.skill, path=dest, dry_run=True)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(body, encoding="utf-8")
    return SkillWriteResult(True, skill=parse_skill(dest) or res.skill, path=dest)


def _is_available(skill: Skill) -> bool:
    """Check the `requires` block against the current machine."""
    req = skill.requires or {}
    bins = req.get("bins") or []
    if isinstance(bins, list):
        for b in bins:
            if not shutil.which(str(b)):
                return False
    apps = req.get("apps") or []
    if isinstance(apps, list):
        from pathlib import Path as _P
        for a in apps:
            if not _P(f"/Applications/{a}.app").exists() \
               and not _P(f"/System/Applications/{a}.app").exists() \
               and not _P(f"/Applications/Utilities/{a}.app").exists():
                return False
    return True


def discover(root: Path) -> list[Skill]:
    """Recursively find all SKILL.md files under `root`."""
    if not root.exists():
        return []
    out: list[Skill] = []
    for p in sorted(root.rglob("SKILL.md")):
        s = parse_skill(p)
        if s is not None:
            out.append(s)
    return out


def load_available(root: Path) -> list[Skill]:
    return [s for s in discover(root) if _is_available(s)]


def _flatten(groups: list[list[Skill]] | list[Skill]) -> list[Skill]:
    if not groups:
        return []
    if groups and isinstance(groups[0], Skill):
        return list(groups)            # type: ignore[arg-type]
    return [s for g in groups for s in g]       # type: ignore[union-attr]


def canonical_app_name(app_name: str) -> str:
    raw = (app_name or "").strip()
    return _APP_ALIASES.get(raw, _APP_ALIASES.get(raw.lower(), raw))


def slugify_app(app_name: str) -> str:
    import unicodedata
    name = canonical_app_name(app_name)
    ascii_name = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    slug = re.sub(r"[^a-z0-9]+", "-", ascii_name.lower()).strip("-")
    return slug or "app"


def canonical_skill_name(app_name: str, family: str = "cu") -> str:
    slug = slugify_app(app_name)
    return f"{slug}-mac" if family == "cu" else slug


def find_skill_for_app(groups: list[list[Skill]] | list[Skill],
                       app_name: str,
                       preferred_names: list[str] | None = None) -> Skill | None:
    """Find the one durable skill that should own facts about this app."""
    flat = _flatten(groups)
    if not flat:
        return None
    prefs = [(p or "").strip().lower() for p in (preferred_names or []) if (p or "").strip()]
    for pref in prefs:
        for s in flat:
            if s.name.lower() == pref:
                return s

    canon = canonical_app_name(app_name)
    canon_slug = slugify_app(canon)
    target_name = canonical_skill_name(canon)
    for s in flat:
        apps = (s.requires or {}).get("apps") or []
        if not isinstance(apps, list):
            continue
        for app in apps:
            if canonical_app_name(str(app)).lower() == canon.lower():
                return s
            if slugify_app(str(app)) == canon_slug:
                return s
    for s in flat:
        if s.name.lower() == target_name:
            return s
    return None


def _round_robin_by_family(skills: list[Skill]) -> list[Skill]:
    """Within one priority group, interleave one skill per family per
    pass so a single huge family doesn't monopolise the budget."""
    by_family: dict[str, list[Skill]] = {}
    family_order: list[str] = []
    for s in skills:
        fam = s.family or "misc"
        if fam not in by_family:
            by_family[fam] = []
            family_order.append(fam)
        by_family[fam].append(s)
    out: list[Skill] = []
    while any(by_family[f] for f in family_order):
        for f in family_order:
            if by_family[f]:
                out.append(by_family[f].pop(0))
    return out


def find_skill(groups: list[list[Skill]] | list[Skill],
               name: str) -> Skill | None:
    """Look up a skill by name across priority groups (first match wins)."""
    if not groups:
        return None
    flat = _flatten(groups)
    nm = (name or "").strip().lower()
    if not nm:
        return None
    for s in flat:
        if s.name.lower() == nm:
            return s
    return None


def render_skill_index(groups: list[list[Skill]] | list[Skill]) -> str:
    """Render a one-line-per-skill INDEX (name + description only).

    Used in the system prompt so the model knows what skills exist
    without paying the body cost. Use `read_skill <name>` at runtime
    to fetch a specific skill's full body when (and only when) the
    task clearly calls for it.
    """
    if not groups:
        return ""
    flat = _flatten(groups)
    if not flat:
        return ""
    lines = ["## Skills available (lazy)\n",
             "Each entry below is a CHEAT-SHEET for one specific app or",
             "CLI. The full body is NOT in this prompt. If exactly one",
             "skill clearly applies to the task, fetch it with",
             '`{"action":"read_skill","skill_name":"..."}` then act on it.',
             "If none clearly apply, do NOT read any — use your tools",
             "directly.\n"]
    for s in flat:
        lines.append(f"- **{s.name}** ({s.family or 'misc'}) — {s.description}")
    return "\n".join(lines) + "\n"


def render_for_prompt(groups: list[list[Skill]] | list[Skill],
                      max_chars: int = 30_000) -> str:
    """Concatenate skill bodies into a single markdown block for the
    system prompt, with a soft size cap.

    The first argument is one or more priority groups (highest first):
    typically [user_skills, bundled_skills] so user-installed skills
    consume the budget first. Within a group, skills are family
    round-robin'd so a huge family doesn't monopolise that group's
    share of the cap.

    Backward-compat: if a flat ``list[Skill]`` is passed, it's treated
    as a single priority group.
    """
    if not groups:
        return ""
    # Detect flat list vs list of lists
    if groups and isinstance(groups[0], Skill):
        groups_norm: list[list[Skill]] = [groups]   # type: ignore[list-item]
    else:
        groups_norm = list(groups)                  # type: ignore[assignment]

    # Build the emission order: each group's skills round-robin'd by
    # family, groups concatenated in priority order.
    ordered: list[Skill] = []
    for g in groups_norm:
        ordered.extend(_round_robin_by_family(g))
    if not ordered:
        return ""

    parts = ["## Skills available\n",
             "Each skill below documents one specific app or CLI integration",
             "where the exact syntax matters and is hard to recall (e.g.",
             "AppleScript dialects). They are NOT general recipes.\n",
             "If exactly one skill clearly applies to the task, follow it.",
             "If several could apply, pick the most specific. If none clearly",
             "apply, do NOT consult any of them — use your tools directly.\n"]
    used = sum(len(p) + 1 for p in parts)
    for i, s in enumerate(ordered):
        block = (
            f"\n### {s.name}  ({s.family or 'misc'})\n"
            f"{s.description}\n\n"
            f"{s.body}\n"
        )
        if used + len(block) > max_chars:
            parts.append(f"\n_(skipped {len(ordered) - i} more skill(s) — prompt size cap)_\n")
            break
        parts.append(block)
        used += len(block)
    return "".join(parts)
