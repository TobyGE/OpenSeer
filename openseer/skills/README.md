# Skills

A *skill* is a Markdown file that teaches the agent how to use a specific
CLI or app. The agent loop scans `skills/` at startup, checks each
skill's `requires:` block against the current machine, and injects the
available skill bodies into the system prompt.

There is **no plugin code**. Adding a skill = writing a Markdown file
with YAML front-matter. This format is borrowed (with attribution) from
[OpenClaw](https://github.com/openclaw/openclaw); skills authored for
either project are largely interchangeable.

## Layout

```
skills/
├── bash/                    # skills the agent uses via the `bash` tool
│   ├── open-url-or-app/SKILL.md
│   ├── clipboard/SKILL.md
│   └── find-files/SKILL.md
└── cu/                      # skills using mouse/keyboard primitives
    ├── macos-dock/SKILL.md
    ├── macos-calculator/SKILL.md
    └── youtube-player/SKILL.md
```

## Front-matter schema

```yaml
---
name: macos-calculator             # short id, kebab-case
description: One-liner for the prompt summary.
family: cu                         # bash | cu | mixed
requires:
  bins: [open, pbcopy]             # required on PATH
  apps: [Calculator]               # required in /Applications
---
```

`requires` is checked at load time. Missing → skill is skipped (the
agent doesn't see it, so it can't try to use missing tools).

## Body conventions

- Lead with one or two paragraphs explaining when this skill is
  useful (the agent reads these to decide whether to consult it).
- Show concrete commands or action JSON examples — the agent will
  copy patterns directly.
- Be opinionated about anti-patterns ("don't click the Dock") so the
  agent learns what *not* to do.

## Adding your own

Just drop a new folder + `SKILL.md` and it'll appear in the next run.
