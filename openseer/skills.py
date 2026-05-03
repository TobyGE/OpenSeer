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


_FRONTMATTER_RE = re.compile(
    r"\A---\s*\n(.*?)\n---\s*\n(.*)\Z", re.DOTALL,
)


def _parse_yaml_lite(text: str) -> dict[str, Any]:
    """Tiny YAML subset: top-level scalars, simple lists, single-level nests.

    Avoids pulling in PyYAML for a one-purpose tool. If a future skill
    needs richer YAML, swap to PyYAML later.
    """
    out: dict[str, Any] = {}
    cur_key: str | None = None
    cur_indent = -1
    current_dict: dict | None = None
    for raw in text.splitlines():
        line = raw.rstrip()
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        indent = len(line) - len(line.lstrip())
        s = line.strip()
        # nested dict mode
        if current_dict is not None and indent > cur_indent:
            if ":" in s:
                k, _, v = s.partition(":")
                v = v.strip()
                if v == "":
                    current_dict[k.strip()] = {}
                elif v.startswith("[") and v.endswith("]"):
                    current_dict[k.strip()] = _parse_inline_list(v)
                else:
                    current_dict[k.strip()] = _strip_quotes(v)
            continue
        # back to top
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


def parse_skill(path: Path) -> Skill | None:
    text = path.read_text(encoding="utf-8")
    m = _FRONTMATTER_RE.match(text)
    if not m:
        return None
    front = _parse_yaml_lite(m.group(1))
    body = m.group(2).strip()
    name = str(front.get("name", path.parent.name))
    return Skill(
        name=name,
        description=str(front.get("description", "")),
        family=str(front.get("family", "")),
        body=body,
        requires=front.get("requires", {}) if isinstance(front.get("requires"), dict) else {},
        path=path,
    )


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


def render_for_prompt(skills: list[Skill], max_chars: int = 20_000) -> str:
    """Concatenate skill bodies into a single markdown block for the
    system prompt. Caps total size to avoid prompt bloat."""
    if not skills:
        return ""
    parts = ["## Skills available\n",
             "These domain knowledge documents tell you how to use specific",
             "CLIs (via `bash`) or apps (via CU primitives). Consult them",
             "before guessing. They are loaded only if their dependencies",
             "exist on this machine, so anything listed below is usable.\n"]
    used = sum(len(p) + 1 for p in parts)
    for s in skills:
        block = (
            f"\n### {s.name}  ({s.family or 'misc'})\n"
            f"{s.description}\n\n"
            f"{s.body}\n"
        )
        if used + len(block) > max_chars:
            parts.append(f"\n_(skipped {len(skills) - skills.index(s)} more skill(s) — prompt size cap)_\n")
            break
        parts.append(block)
        used += len(block)
    return "".join(parts)
