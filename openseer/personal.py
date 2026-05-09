"""Per-user personality + durable memory for OpenSeer.

Two plain Markdown files OpenSeer reads at the start of every run:

- ``~/.openseer/SOUL.md``  — voice / tone / how the agent talks. Persona-level
  guidance that shapes replies. Edit anytime.
- ``~/.openseer/MEMORY.md`` — durable facts about the user (preferences,
  payment defaults, addresses, learned decisions). Read when deciding HOW to
  do a task: "look up payment-card before asking which card to use".

Design choices borrowed from OpenClaw's SOUL.md / MEMORY.md doc, kept
minimal: one file each, plain Markdown, full-text injection into the system
prompt every run. No vector search, no daily-notes rotation, no dreaming
sweep — those live in heavier memory systems.

Files default to a small starter template if missing. The setup wizard
seeds them on first run; otherwise lazy creation happens on first read.

Public API:
  - SOUL_PATH / MEMORY_PATH: filesystem locations
  - load_soul() / load_memory(): read text (creating from template if absent)
  - render_personal_block(): the chunk that gets appended to the system prompt
  - append_memory(line): atomic append to MEMORY.md (used by the
    ``write_memory`` action and reflection-proposed updates)
  - seed_defaults_if_missing(): idempotent first-run install
"""
from __future__ import annotations

import os
from pathlib import Path
from threading import Lock


_OPENSEER_DIR = Path.home() / ".openseer"
SOUL_PATH = _OPENSEER_DIR / "SOUL.md"
MEMORY_PATH = _OPENSEER_DIR / "MEMORY.md"

# Hard cap on what we'll inject into the system prompt. Either file growing
# past this without a corresponding curation pass would silently push other
# instructions out of attention.
_MAX_FILE_BYTES = 16 * 1024


_SOUL_TEMPLATE = """\
# OpenSeer voice

This file shapes how OpenSeer talks to you. Edit anytime.

- Be concise. One short paragraph beats five. No corporate filler
  ("Great question", "I'd be happy to help", "Absolutely").
- Match the user's language. Reply in 中文 when the user writes 中文,
  in English when English, mix the same way they do.
- Decision points where I clearly have a preference — picking a
  specific seat, a payment card, a shipping address, which item from
  a list to add to cart, which date/time to book — STOP and ask the
  user. Use the run's interactive mechanism (the system prompt
  describes it) with a screenshot of the current screen attached.
  Don't pick on my behalf and don't substitute `terminate(fail)` for
  the question — the run should *pause* for my answer and *resume*,
  not end.
- Hard-to-reverse actions (paying, sending, posting, deleting) ALWAYS
  need explicit confirmation right before the click, with a screenshot.
- Use MEMORY.md as the source of truth for my preferences. Don't ask
  the same question twice — read MEMORY.md first, only ask when
  nothing applicable is there.
- Show your reasoning in `thought` honestly — never claim success
  when the screen disagrees.
"""


_MEMORY_TEMPLATE = """\
# Durable memory

OpenSeer reads this file at the start of every run. Anything here is
treated as known fact about the user — the agent uses these to skip
asking questions it already has answers to.

Add or edit entries freely. OpenSeer can also append here when you
confirm via the "Apply" button on Telegram.

## Format

Plain Markdown. Use bullet points or short sections. The model parses
it as text, so structure is up to you.

## Examples (delete what doesn't apply, add what does)

### Payment
- (none yet — set a default card here, e.g. "AMEX ending 1234")

### Shipping / address
- (none yet)

### Preferences
- (none yet — e.g. "movie seats: rear row, center")

### Boundaries
- Never complete a payment, send a message, post publicly, or delete
  anything without first surfacing the action for user approval — see
  the system prompt for the exact mechanism available this run
  (ask_user when wired, otherwise terminate(fail) with the question
  in the reason).
"""


_write_lock = Lock()


def _ensure_dir() -> None:
    _OPENSEER_DIR.mkdir(parents=True, exist_ok=True)


def _read_or_seed(path: Path, template: str) -> str:
    """Read ``path``, creating it from ``template`` if missing.

    Files in ``~/.openseer/`` may contain card last-fours, addresses,
    and similar lightly-sensitive data; set 0600 on creation. We don't
    chmod existing files (the user may have set their own perms).
    """
    if not path.exists():
        _ensure_dir()
        try:
            path.write_text(template, encoding="utf-8")
            try:
                os.chmod(path, 0o600)
            except OSError:
                pass
        except OSError:
            return template
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return template


def load_soul() -> str:
    return _read_or_seed(SOUL_PATH, _SOUL_TEMPLATE)


def load_memory() -> str:
    return _read_or_seed(MEMORY_PATH, _MEMORY_TEMPLATE)


def seed_defaults_if_missing() -> tuple[bool, bool]:
    """First-run install. Returns (soul_seeded, memory_seeded) so the setup
    wizard can tell the user what was created."""
    _ensure_dir()
    soul_new = not SOUL_PATH.exists()
    mem_new = not MEMORY_PATH.exists()
    if soul_new:
        load_soul()
    if mem_new:
        load_memory()
    return (soul_new, mem_new)


def render_personal_block() -> str:
    """Build the SOUL + MEMORY chunk for injection into the system prompt.

    Both files are quoted in full but each capped at ``_MAX_FILE_BYTES``;
    a truncation marker tells the user-side reflection pass to suggest
    curation when the cap is hit.
    """
    soul = load_soul()
    memory = load_memory()

    def _cap(text: str, label: str) -> str:
        if len(text.encode("utf-8")) <= _MAX_FILE_BYTES:
            return text
        head = text.encode("utf-8")[: _MAX_FILE_BYTES].decode("utf-8", errors="ignore")
        return head + (
            f"\n\n[... {label} truncated at {_MAX_FILE_BYTES} bytes — "
            f"consider trimming this file.]"
        )

    soul = _cap(soul, "SOUL.md").strip()
    memory = _cap(memory, "MEMORY.md").strip()

    parts = []
    if soul:
        parts.append(
            "## Personality (SOUL.md — how to talk)\n\n" + soul
        )
    if memory:
        parts.append(
            "## Durable memory (MEMORY.md — what you already know about the user)\n\n"
            + memory
        )
    if not parts:
        return ""
    return "\n\n".join(parts)


def append_memory(entry: str) -> None:
    """Append a single Markdown bullet (or block) to MEMORY.md.

    The caller passes ``entry`` already formatted (e.g.
    ``- payment: AMEX ending 1234 (default)``). This is the single
    atomic write path used by both the model's ``write_memory`` action
    and the reflection callback's "Apply memory update" button.
    """
    if not entry.strip():
        return
    _ensure_dir()
    with _write_lock:
        # Make sure file exists with template if first write.
        load_memory()
        # Append, ensuring exactly one blank line of separation.
        sep = "\n\n" if MEMORY_PATH.read_text(encoding="utf-8").rstrip().endswith(
            ("-", ":", ".", ")", "]")
        ) else "\n\n"
        with MEMORY_PATH.open("a", encoding="utf-8") as f:
            f.write(sep)
            f.write(entry.rstrip())
            f.write("\n")
