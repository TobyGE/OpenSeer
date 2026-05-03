# OpenSeer

> **Sees. Remembers. Acts.**
>
> Computer-use, but it actually knows you.

OpenSeer is an open-source, local-first personal assistant for macOS.
It can see your screen, drive your apps with mouse and keyboard, and
remember what you've done — driven by LLMs you choose.

Most "computer-use" agents today are stateless task executors: start
from blank, forget on exit. OpenSeer is built around the opposite
premise — **the assistant should know you**. The long-term goal is to
maintain memory of your machine state, learn preferences from how you
actually work, and use that context to plan and verify each action.

> Status: **pre-alpha**. Working internals, reckless edges. Not for
> daily use yet — it will literally take over your mouse and keyboard.

## Why this exists

| Existing tool                 | What it gives you            | What's missing                      |
|-------------------------------|------------------------------|-------------------------------------|
| Claude / ChatGPT (web/app)    | Great conversation           | Can't drive your Mac                |
| Anthropic computer-use tool   | Pixel-level GUI control      | No memory; blank every session      |
| Codex desktop                 | Codes well                   | Coding only                         |
| OpenClaw                      | Persona, channels, memory    | Doesn't actually operate GUIs       |
| trycua/cua                    | Solid execution substrate    | No assistant layer — you build it   |

OpenSeer aims at the layer those products are missing: chat-first,
memory-aware, locally-running, model-agnostic, open source.

## What works today

- Full-screen capture via Quartz (`CGWindowListCreateImage`-style)
- Multi-turn agent loop driven by GPT-5.5 (via the user's ChatGPT
  subscription OAuth — no API key required)
- Pluggable grounding backends (default: GPT-5.5 vision\_json;
  alternative: Anthropic Haiku via OAuth)
- `reground` action — the model can call for a focused grounding pass
  with optional region zoom and an "external/specialist" flag
- `open_app` action — bypass the Dock, launch any app via `open -a`
- Multi-action chains — the model can emit `{"actions":[…]}` to run
  several steps inside one API call (saves round-trips on
  deterministic sequences like "click + hotkey + hotkey + hotkey")
- Verification chain — `done` requires `verified_by_steps` listing
  prior producing actions; pure observation isn't enough
- Token-budget callback + 429/5xx exponential-backoff retry
- Sliding-window image retention (4 most recent frames, older turns
  drop image and keep text summary)
- Per-step trajectory log: raw screenshot, annotated screenshot, full
  multi-turn input, raw model response, complete SSE event stream,
  `transcript.json`, human-readable `trace.md`

## What's coming

- **Memory bridge to PersonalMem** — the wedge: agent that knows what
  you've actually been doing on your Mac
- Skill/macro learning from repeated trajectories
- Multi-channel input (CLI today; CLI + web + iMessage planned)
- Sandbox / permission gates for risky actions
- Additional grounding backends — Claude `computer_20251124` tool,
  OpenAI `computer-use-preview`, self-hosted UI-TARS

## Quick start

```bash
git clone https://github.com/TobyGE/OpenSeer.git
cd OpenSeer
pip install -e .
```

OpenSeer authenticates against the ChatGPT codex backend by reusing
the OAuth tokens that the official **Codex CLI** stores locally —
so your ChatGPT subscription powers the model, no API key required.
Install Codex CLI once, then log in through OpenSeer:

```bash
# 1. install Codex CLI (one-time, system-wide)
npm install -g @openai/codex

# 2. log in — opens your browser, drops a token in ~/.codex/auth.json
openseer auth login

# 3. confirm
openseer auth status
# → ✅ logged in — auth_mode=chatgpt plan=plus expires_in=…h
```

You also need to grant your terminal **Accessibility** permission
(System Settings → Privacy & Security → Accessibility), otherwise
pyautogui will silently fail to inject mouse / keyboard events.

Then run a task:

```bash
openseer "Open Calculator and compute 999 * 123"
# equivalent to: openseer task "Open Calculator and compute 999 * 123"

openseer --execute "Open Calculator and compute 999 * 123"   # actually drive the UI
openseer --execute --confirm-each "..."                       # ask y/s/q per step
```

The agent will take over the mouse and keyboard while it runs. To
abort, slam the cursor into a screen corner (pyautogui FAILSAFE) or
hit `Ctrl+C` in the terminal.

Each run writes a full trajectory to `~/Desktop/openseer/run-{timestamp}/`.

## Architecture

```
┌─────────────────────────────────────┐
│ Brain (LLM router, planner)         │   GPT-5.5 today; Claude/local pluggable
├─────────────────────────────────────┤
│ Tools                               │
│   - Computer-use (screen + click)   │   primary tool, where most action lives
│   - Memory recall (planned)         │
│   - Shell / APIs (planned)          │
├─────────────────────────────────────┤
│ Substrate                           │
│   - Grounder (swappable)            │
│   - Executor (pyautogui)            │
│   - Screen capture (Quartz)         │
└─────────────────────────────────────┘
```

The agent loop is intentionally small: capture → ask model → parse
action(s) → execute → loop. Cross-cutting concerns (image retention,
trajectory logging, budget tracking) plug in as `Callback`s.

## License

Apache 2.0. See [LICENSE](./LICENSE).

## Status

Pre-alpha. The README lists what works, but expect rough edges.
Issues and ideas welcome — but this isn't a product yet.
