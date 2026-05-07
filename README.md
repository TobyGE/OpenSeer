# OpenSeer

> **Sees. Remembers. Acts.**
> Computer-use, but it actually knows you.

Open-source, local-first personal assistant for macOS. Sees your
screen, drives your apps, remembers what you've done — using GPT-5.5
or Claude Haiku 4.5.

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

`setup` detects your existing Codex / Claude Code logins and runs
OAuth for whichever you're missing. Provider choice persists to
`~/.openseer/config.json`; override with `OPENSEER_PROVIDER=…`.

```bash
openseer "Open Calculator and compute 999 * 123"   # one-off
openseer task --execute --confirm-each "..."       # step-by-step
openseer daemon                                    # listen on Telegram
```

In the REPL: `/learn <app>` to research+save a SKILL.md, `/show`,
`/history`, `/context`, `/reset`, `/help`. Suffix flags: `... --dry`,
`... --steps 8`. Abort: corner-slam (pyautogui FAILSAFE) or `Ctrl+C`.
Each run dumps a full trajectory to `~/.openseer/runs/<id>/`.

## What's in

- **Two providers** — GPT-5.5 (Codex OAuth) and Haiku 4.5 (Claude
  Code OAuth). Switch any time.
- **Native macOS AX grounding** — every turn dumps the foreground
  app's accessibility tree as an indexed element list; model clicks
  `index=N` instead of guessing pixels. Works on Catalyst apps where
  AppleScript can't.
- **Action surface** — `bash`, `web_search` / `web_fetch`, `click` /
  `type` / `key` / `scroll`, `open_app`, `wait` / `screenshot` /
  `get_app_state`, `reground`, `read_skill` / `write_skill`,
  `terminate`.
- **Skills** — per-app markdown cheat-sheets, auto-indexed each turn,
  `/learn` writes them via a 4-phase research session.
- **Honest reflection** — every `thought` opens with
  `[SUCCESS|INEFFECTIVE|REGRESSED|N/A]`; chain semantics auto-break
  on state-changing actions so the model sees new state before
  deciding next.
- **Phone → Mac via Telegram** (`openseer daemon`) — per-chat
  multi-turn memory, live progress (ack edits every ~2.5s), image
  attachments back to your phone, fail-closed allowlist.
- **Safety** — `--confirm-each`, bash danger regex, `write_skill`
  body preview confirm, terminal-AX blacklist.
- **Trace** — per-step screenshots, AX tree, raw response, SSE
  events, `transcript.json`, `trace.md`.

## What's coming

- Memory bridge to PersonalMem — an agent that knows what you've
  actually been doing on your Mac
- Macro / shortcut evolution from repeated trajectories
- Skill marketplace
- Web channel (CLI + Telegram today)
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
