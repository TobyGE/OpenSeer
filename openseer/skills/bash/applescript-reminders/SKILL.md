---
name: applescript-reminders
description: Create / complete / list Apple Reminders via osascript. USE THIS for any todo-style request ("提醒我下午 3 点 X", "remind me to Y tomorrow", "加个 todo", "what reminders do I have today?"). Don't click the Reminders UI.
family: bash
requires:
  apps: ['Reminders']
  bins: ['osascript']
---

# Reminders via AppleScript

## Add a simple reminder (no due date)

```bash
osascript -e 'tell application "Reminders" to make new reminder with properties {name:"Pick up groceries"}'
```

## Add a reminder with a due date

```bash
osascript <<'EOF'
tell application "Reminders"
  set dueDate to (current date) + (1 * days)
  set time of dueDate to (9 * hours)        -- 09:00 tomorrow
  make new reminder with properties {name:"Call dentist", remind me date:dueDate}
end tell
EOF
```

Common date math: `(current date) + (n * days)`, `+ (n * hours)`,
`+ (n * minutes)`. Set absolute time with `set time of d to (h * hours)`.

## Add to a specific list (not the default)

```bash
osascript <<'EOF'
tell application "Reminders"
  tell list "Work"
    make new reminder with properties {name:"Submit PR review", body:"Check the codex review tool"}
  end tell
end tell
EOF
```

## List incomplete reminders due TODAY (date-bounded)

The naive `whose completed is false` predicate returns ALL incomplete
reminders, including overdue and future. To get just today's:

```bash
osascript <<'EOF'
tell application "Reminders"
  set today to current date
  set hours of today to 0
  set minutes of today to 0
  set seconds of today to 0
  set tomorrow to today + 1 * days
  set out to ""
  repeat with r in (reminders whose completed is false and remind me date >= today and remind me date < tomorrow)
    set out to out & (name of r) & linefeed
  end repeat
  return out
end tell
EOF
```

If you want ALL incomplete (overdue + future + today), drop the date
predicates — but that is rarely what the user asks for.

## Mark a reminder done by name

```bash
osascript <<'EOF'
tell application "Reminders"
  set r to first reminder whose name is "Call dentist"
  set completed of r to true
end tell
EOF
```

## Tips

- Reminders' default list is whichever the user picked last. To be
  safe when adding, always specify `tell list "..."`.
- `body` is the optional note text shown under the reminder.
- For recurring reminders, set `remind me date` and AppleScript can't
  set repetition rules cleanly — use the GUI for that one case.
