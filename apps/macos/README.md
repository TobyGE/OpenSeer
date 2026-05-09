# OpenSeer macOS app (work in progress)

A SwiftUI shell on top of the existing `openseer` Python CLI. The Swift
app does NOT reimplement any agent logic; it shells out to the same
binary the REPL/daemon/CLI users invoke today.

## Status

**Phase 0 — skeleton (this commit).** Buildable, opens a window, routes
between a placeholder setup wizard and a placeholder chat view based on
`openseer auth status` exit code. No real CLI calls beyond the auth
probe yet.

Subsequent phases:

- Phase 1 — wire up real OAuth / permission / Telegram setup steps.
- Phase 2 — chat window: input box, task subprocess, turn-aggregated
  bubble rendering with disclosure to step detail.
- Phase 3 — daemon panel: start/stop, live tailing of new traces under
  `~/.openseer/runs/`.
- Phase 4 — settings panel: edit SOUL.md / MEMORY.md, view skills,
  manage Telegram allowlist.

## Develop

```bash
cd apps/macos
swift run OpenSeerGUI
```

That builds and launches the app via SPM. Requires the `openseer`
Python CLI on `$PATH` (or installed in the repo's `.venv`).

## Distribution

Not in MVP scope. A signed/notarized `.app` bundle later will need
either an Xcode project + Apple Developer signing or
[`swift-bundler`](https://github.com/stackotter/swift-bundler).
