---
name: macro-from-conversation
description: Interview the user about a workflow they do manually, then `write_skill` from the answers. USE THIS when the user asks you to "record a macro" / "记一下这个流程" / "remember how to X" for a workflow they did NOT just do with you (so the session context doesn't already have the steps).
family: cu
requires:
  apps: []
---

# Recording a macro by interviewing the user

This skill is for the "(b)" sub-case in the system prompt's "User-triggered macros" section: the user wants you to remember a workflow they do **themselves**, that wasn't part of this run. Since you don't have producing-action history to lift from, you have to ask.

Aim for **3-5 short questions max**, not a full survey. The user is busy. Each `ask_user(kind="text")` interrupts them, so batch related fields when natural.

## Interview script

Adapt to the conversation, but cover these in order:

**1. Target app + trigger phrase** (one question, both fields):
```
ask_user(
  kind="text",
  question="What app + what should I call this macro? e.g. 'WeChat, send-good-morning'."
)
```
Parse the reply: first token before comma or space is the app, the rest is the macro name. If ambiguous, ask_user once more for the missing piece.

**2. The steps** (one question, multi-line answer):
```
ask_user(
  kind="text",
  question="Walk me through the steps — what do you click / type / read, in order? Plain language, one line per step is fine. (I'll handle the AX details.)"
)
```
Free-form answer. Don't expect bash-like precision — translate the human description into a probable sequence of agent actions in the skill body.

**3. Success criteria** (one question):
```
ask_user(
  kind="text",
  question="How do you know when it's done correctly? What does the screen look like at the end?"
)
```
This becomes the verification step the skill recommends to future runs.

**4. Footguns** (one question, optional — skip if user seems annoyed):
```
ask_user(
  kind="text",
  question="Anything that's tripped you up before — a dialog that pops up, a button that's easy to misclick, a key combo that doesn't work?"
)
```
These become the "Footguns observed" section of the skill body.

## Writing the SKILL.md from the answers

Template the body roughly like this (keep section headers, fill from the answers):

```markdown
---
name: <macro-name-from-Q1>
description: USE THIS when the user asks to <Q1 macro name in plain words>. <one sentence from Q3 about what success looks like>.
family: cu
requires:
  apps: ['<app from Q1>']
---

# <macro name>

## When to use this

<one paragraph paraphrasing Q1 trigger context>

## Standard flow

<numbered list translated from Q2; each step references an action type
the agent uses: open_app / click index= / type text= / key / bash etc.
Where you're guessing because the user was vague, mark with [PROBABLE]
so the next run can correct it.>

## Verifying

<from Q3>

## Footguns observed

<from Q4, or "(none reported)" if skipped>

## Status

[UNVERIFIED] Recorded from user description on YYYY-MM-DD; the flow
above hasn't been executed by the agent end-to-end yet. The first
run that uses this skill should refine any [PROBABLE] markers above
based on what actually worked.
```

The `[UNVERIFIED]` tag matters: future runs treat the macro as a
starting point, not gospel. As soon as a real run completes
end-to-end successfully, `write_skill` REPLACES the file with the
refined version (auto-trigger path, ≥4 actions + SUCCESS chain).

## Confirm before writing

Before calling `write_skill`:
```
ask_user(
  kind="confirm",
  question="Here's what I'm about to save as the '<name>' macro.
            <show the body inline>
            Save?"
)
```

The executor surfaces a body preview confirm anyway, but a chat-side
confirm is friendlier. If user says No, ask "what should I change?"
and iterate.

## When NOT to use this skill

- The workflow JUST happened in this run with you driving — write the skill directly from session context, no interview needed (auto-trigger path in the system prompt).
- The user is just asking a one-off question, not asking to save a workflow. Don't interview if they didn't say "macro / 记下 / save as / remember".
- The "workflow" is really just one or two actions (open an app, click one button) — that's not skill-worthy. Tell the user "this is a one-liner, you don't really need a saved macro for it".
