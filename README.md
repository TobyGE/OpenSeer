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
- Pluggable grounding backend (default: GPT-5.5 vision\_json; more
  backends planned — Anthropic CU tool, OpenAI CUA, self-hosted UI-TARS)
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

First time, run the guided setup — it walks you through Codex CLI install,
OAuth login, and the two macOS permissions OpenSeer needs:

```bash
openseer setup
```

What it checks:

```
[1/5] Codex CLI installed
[2/5] ChatGPT OAuth login (no API key needed — uses your subscription)
[3/5] macOS Accessibility permission   (lets us inject mouse/keyboard)
[4/5] macOS Screen Recording permission (lets us see the screen)
[5/5] Smoke test (1 model ping, no UI actions)
```

Manual subcommands if you ever need to redo just one step:

```bash
openseer auth login     # re-run the OAuth dance
openseer auth status    # check token validity
openseer auth logout    # wipe local tokens
```

Then drop into the chat shell:

```bash
$ openseer
OpenSeer — Sees. Remembers. Acts.
  Type a task, or /help for commands, /exit to leave.
  logged in: plus

openseer ❯ Open Calculator and compute 999 * 123
[task] Open Calculator and compute 999 * 123
...
[finished] 6 step(s) in 31.4s — last: done

openseer ❯ /history
  run-20260502-211052  Open Safari, go to youtube.com, search for ...
  run-20260502-205834  Open Calculator, compute 17 * 42, copy the ...

openseer ❯ /exit
```

Suffix flags work per-task: `Open Notes --dry`, `Find foo --steps 8 --confirm`.

Or run one-off without the REPL:

```bash
openseer "Open Calculator and compute 999 * 123"
# equivalent to: openseer task "Open Calculator and compute 999 * 123"

openseer task --execute --confirm-each "..."   # actually drive the UI, step-confirm
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
