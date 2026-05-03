# OpenSeer

> **Sees. Remembers. Acts.**
> Computer-use, but it actually knows you.

An open-source, local-first personal assistant for macOS. Sees your
screen, drives your apps, remembers what you've done.

> **Status: pre-alpha.** Working internals, reckless edges. It will
> literally take over your mouse and keyboard.

## Quick start

```bash
git clone https://github.com/TobyGE/OpenSeer.git
cd OpenSeer
pip install -e .

openseer setup        # guided onboarding (Codex CLI, OAuth, macOS perms)
openseer              # drop into the chat shell
```

Inside the shell:

```
openseer ❯ Open Calculator and compute 999 * 123
  ▸ Open Calculator and compute 999 * 123

  ● Launch Calculator without disturbing windows
     └ open Calculator ✓
  ● Type the expression and press equals
     └ key 1, 7, *, 4, 2, enter (chain ×6) ✓
  ...
  ✓ done  6 steps · 14.2s · 18,775 in / 1,629 out · ~$0.016
        ↳ run-20260502-211052

openseer ❯ /history     # past runs
openseer ❯ /exit
```

One-off (no REPL):

```bash
openseer "Open Calculator and compute 999 * 123"
openseer task --execute --confirm-each "..."   # step-by-step confirm
```

Suffix flags inside the REPL: `Open Notes --dry`, `Find foo --steps 8`.

To abort mid-task: slam cursor into a screen corner (pyautogui FAILSAFE)
or `Ctrl+C` in the terminal.

Each run writes a full trajectory to `~/Desktop/openseer/run-{timestamp}/`.

## What's in

- Multi-turn agent loop driven by GPT-5.5 (ChatGPT subscription OAuth — no API key)
- Pluggable grounder (default: GPT-5.5 vision\_json)
- `reground` — model-initiated focused grounding with optional region zoom
- `open_app` — bypass the Dock via `open -a`
- Multi-action chains — `{"actions":[…]}` saves round-trips on hotkey sequences
- Verification chain — `done` must cite producing steps; observation alone isn't enough
- Sliding-window image retention (4 most recent frames; older turns text-only)
- 429/5xx exponential-backoff retry, token-budget callback
- Per-step trajectory: screenshots, full multi-turn input, raw response, SSE events,
  `transcript.json`, `trace.md`

## What's coming

- **Memory bridge to PersonalMem** — the wedge: an agent that knows
  what you've actually been doing on your Mac
- Skill / macro learning from repeated trajectories
- Multi-channel input (CLI today; web + iMessage planned)
- Sandbox / permission gates for risky actions
- More grounders — Claude `computer_20251124`, OpenAI `computer-use-preview`,
  self-hosted UI-TARS

## Architecture

```
Brain (LLM planner)                — GPT-5.5; Claude / local pluggable
   │
Tools  ── computer-use ── memory recall (planned) ── shell (planned)
   │
Substrate  ── Grounder ── Executor (pyautogui) ── Screen capture (Quartz)
```

Agent loop is intentionally small: **capture → ask model → parse
action(s) → execute → loop**. Cross-cutting concerns (image retention,
trajectory, budget) plug in as `Callback`s.

## License

Apache 2.0. See [LICENSE](./LICENSE).
