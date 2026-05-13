# OpenSeer

Open-source, local-first computer-use agent for macOS. Sees your
screen, drives your apps, remembers what you've done — powered by
GPT-5.5 or Claude Haiku 4.5.

**Pre-alpha.** Working internals, reckless edges. It will literally
take over your mouse and keyboard.

## Install

### macOS app (recommended)

Download `OpenSeer-x.y.z.dmg` from the
[Releases](https://github.com/TobyGE/OpenSeer/releases) page, drag
`OpenSeer.app` into `/Applications`, launch it once, and complete
the OAuth + Privacy prompts that the in-app setup wizard walks you
through. The bundle ships its own Python; nothing to install.

If macOS won't open it ("unidentified developer" — we're not
notarized yet), right-click → Open the first time, or:

```bash
xattr -d com.apple.quarantine /Applications/OpenSeer.app
```

### From source

```bash
git clone https://github.com/TobyGE/OpenSeer.git
cd OpenSeer
pip install -e .

openseer setup        # pick provider, OAuth, grant macOS perms
openseer              # drop into the chat shell
```

`setup` detects your existing Codex / Claude Code logins and runs
OAuth for whichever you're missing. Provider choice persists to
`~/.openseer/config.json`; override with `OPENSEER_PROVIDER=…`.

## How you drive it

- **Main window** — long-form chat thread, full history, click into
  any past run to see steps + thoughts + screenshots.
- **Floating voice orb** — `cmd+option+S` from anywhere on the
  system summons a small crystal-ball window at the bottom-right of
  your screen with mic on. Press the same hotkey again to dismiss
  it entirely (panel hidden, mic silenced — no invisible recording).
  The orb captures whatever app you were just looking at as task
  context.
- **Hand off mid-run** — the orb's *Hand off* button parks the
  agent between steps so you can take the mouse for a few moves;
  *Resume* picks back up with the new state.
- **Telegram** — `openseer daemon` exposes the same agent to your
  phone with per-chat memory, live progress, and image attachments.

## What's in

- **Two providers** — GPT-5.5 (Codex OAuth) and Haiku 4.5 (Claude
  Code OAuth). Switch any time.
- **Native macOS AX grounding** — every turn dumps the foreground
  app's accessibility tree as an indexed element list; model clicks
  `index=N` instead of guessing pixels. Lifted out as a standalone
  [`openseer_ax`](./openseer_ax) package so other macOS automation
  projects can reuse it.
- **Action surface** — `bash`, `web_search` / `web_fetch` /
  `read_page`, `click` / `type` / `key` / `scroll`, `open_app`,
  `wait` / `screenshot` / `get_app_state`, `reground`, `read_skill`
  / `write_skill`, `terminate`.
- **Skills that grow themselves** — per-app / per-site markdown
  cheat-sheets, auto-indexed each turn. After every task,
  reflection looks at this run **plus up to 5 recent runs on the
  same site/app** and proposes a skill update if a pattern recurs.
  The orb's main chat thread surfaces it as a "Learned something —
  save as a skill?" chip with Preview / Save / Discard.
- **Honest reflection** — every `thought` opens with
  `[SUCCESS|INEFFECTIVE|REGRESSED|THINKING]`; chain semantics
  auto-break on state-changing actions so the model sees new state
  before deciding next.
- **Background clicks** — `CGEventPostToPid` routes clicks to the
  target app without stealing your cursor (ported from Peekaboo).
- **MCP server** — `openseer mcp serve` exposes OpenSeer's
  primitives (screenshot, click, type, key, scroll, open_app,
  get_app_state) to any MCP-compatible host (Codex CLI, Claude
  Code, Cursor).
- **Safety** — `--confirm-each`, bash danger regex, `write_skill`
  body preview confirm, terminal-AX blacklist.
- **Trace** — per-step screenshots, AX tree, raw response, SSE
  events, `transcript.json`, `trace.md`.

## What's coming

- Memory bridge to PersonalMem — an agent that knows what you've
  actually been doing on your Mac
- Macro / shortcut evolution from repeated trajectories
- Skill marketplace
- Web channel
- More grounders — Claude `computer_20251124`, OpenAI
  `computer-use-preview`, self-hosted UI-TARS

## Contributing

```bash
git config core.hooksPath .githooks
```

`pre-push` runs `codex review` against pushed commits (advisory).
Set `OPENSEER_SKIP_REVIEW=1` to skip.

## License

Apache 2.0. See [LICENSE](./LICENSE).
