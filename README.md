# OpenSeer

> **Sees. Remembers. Acts.**
> Computer-use, but it actually knows you.

An open-source, local-first personal assistant for macOS. Sees your
screen, drives your apps, remembers what you've done — using GPT-5.5
or Claude Haiku 4.5 via your existing subscription (no API key
required).

> **Status: pre-alpha.** Working internals, reckless edges. It will
> literally take over your mouse and keyboard.

## Quick start

```bash
git clone https://github.com/TobyGE/OpenSeer.git
cd OpenSeer
pip install -e .

openseer setup        # pick provider, OAuth, grant macOS perms
openseer              # drop into the chat shell
```

`openseer setup` detects which providers you're already signed into
(Codex CLI for GPT-5.5, Claude Code for Haiku 4.5) and offers to
launch the OAuth flow for whichever isn't yet logged in. Your choice
persists to `~/.openseer/config.json`. Override per-shell with
`OPENSEER_PROVIDER=anthropic openseer`.

Inside the shell:

```
openseer ❯ Open Calculator and compute 999 * 123
  ▸ Open Calculator and compute 999 * 123

  ● [N/A] first turn. Next: open Calculator.
     └ open Calculator ✓
  ● [SUCCESS] Calculator launched. Next: type expression.
     └ key 9, 9, 9, *, 1, 2, 3, enter (chain ×8) ✓
  ● [SUCCESS] result 122877 visible. Next: terminate done.
     └ terminate (done) 999 × 123 = 122877.

  ✓ done  3 steps · 8.4s · 12,300 in / 410 out · ~$0.005
        ↳ a8b656e3
```

REPL slash commands:

```
/learn <app>     research an app via web + exploration, save a SKILL.md
/show [last|<id>]  expand a past run's transcript
/history [N]     last N runs
/context         what session memory the next task will see
/reset           clear session memory
/help            full list
```

One-off (no REPL):

```bash
openseer "Open Calculator and compute 999 * 123"
openseer task --execute --confirm-each "..."   # step-by-step confirm
```

Suffix flags inside the REPL: `Open Notes --dry`, `Find foo --steps 8`.

To abort mid-task: slam cursor into a screen corner (pyautogui FAILSAFE)
or `Ctrl+C` in the terminal.

Each run writes a full trajectory to `~/.openseer/runs/<id>/`.

## What's in

**Two model providers** (pick at setup, switch any time):
- **OpenAI GPT-5.5** via Codex CLI OAuth — your ChatGPT subscription
- **Anthropic Claude Haiku 4.5** via Claude Code OAuth — your Claude subscription

**Native grounding via macOS Accessibility tree.** Each turn the agent
captures a screenshot AND dumps the foreground app's a11y tree. The
model gets an indexed list of every interactive element (buttons,
text fields, tabs) with labels and bboxes — and clicks by `index=N`
instead of guessing pixel coordinates. Works on Catalyst apps too
(WeChat Reading, Books, etc.) where AppleScript and pixel-based
grounding both struggle.

**Action surface**:
- `bash` — shell commands
- `web_search` / `web_fetch` — search (Tavily / Brave / DuckDuckGo fallback) + fetch URLs
- `click` / `type` / `key` / `scroll` — by `index` (AX) or `x,y` (pixels)
- `open_app` — bypass the Dock; uses `open -a` + osascript activate
- `wait` / `screenshot` / `get_app_state` — observe / refresh / re-focus
- `reground` — model-initiated focused (re-)grounding with optional region zoom
- `read_skill` / `write_skill` — load / persist app-specific cheat-sheets
- `terminate` — done (with `verified_by_steps` citation) or fail

**Skill system** — markdown cheat-sheets per app:
- Auto-loaded into prompt as a one-line index per turn
- `read_skill <name>` fetches the full body when relevant
- `/learn <app>` runs a 4-phase research-and-explore session, then
  `write_skill` persists what was learned for next time

**Honest reflection loop**: every model `thought` starts with
`[SUCCESS|INEFFECTIVE|REGRESSED|N/A]` so the model self-corrects
instead of hallucinating progress.

**State-aware chain semantics**: the model can chain actions when
they're deterministic in the current state (cmd+a → type → enter),
but auto-breaks the chain after any state-changing or data-returning
action so the model sees the new state before deciding next.

**Trace + observability**: per-step screenshots, AX tree, full
multi-turn input, raw response, SSE events, `transcript.json`,
`trace.md`. Streaming `thought` field rendered live in the REPL while
the model thinks.

**Safety & guards**:
- Per-action confirmation mode (`--confirm-each`)
- Risky-action awareness in prompt (CC-style reversibility framing)
- `write_skill` always requires user confirm with body preview
- Bash danger regex (`rm -rf`, `curl | sh`, etc.)
- Terminal-app blacklist for AX (won't click into OpenSeer's own iTerm)

## Architecture

```
Brain (LLM planner)         GPT-5.5 (Codex OAuth) | Haiku 4.5 (Claude OAuth)
   │
Perception ──── screenshot (Quartz) + AX tree (PyObjC ApplicationServices)
   │
Tools ──── bash + web_search/web_fetch + CU primitives + skills + control
   │
Executor ──── pyautogui (with NSPasteboard for CJK input) + osascript
```

Agent loop: **capture → AX dump → ask model → parse action(s) →
execute → loop**. Cross-cutting concerns (image retention, trajectory,
budget, safety) plug in as `Callback`s. Provider-specific payload
shaping happens in `openai_chatgpt.py` / `anthropic_messages.py`; the
agent loop is provider-agnostic.

## What's coming

- **Memory bridge to PersonalMem** — the wedge: an agent that knows
  what you've actually been doing on your Mac
- Macro / shortcut evolution from repeated trajectories (AppAgentX-style)
- Skill marketplace
- Multi-channel input (CLI today; web + iMessage planned)
- More grounders — Claude `computer_20251124`, OpenAI
  `computer-use-preview`, self-hosted UI-TARS

## Contributing

Tracked git hooks live in `.githooks/`. Enable them per-clone with:

```bash
git config core.hooksPath .githooks
```

`pre-push` runs `codex review` against the commits being pushed and
prints findings (advisory; doesn't block). Set `OPENSEER_SKIP_REVIEW=1`
to skip on a one-off basis.

## License

Apache 2.0. See [LICENSE](./LICENSE).
