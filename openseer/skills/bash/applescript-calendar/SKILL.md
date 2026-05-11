---
name: applescript-calendar
description: Create / move / query Apple Calendar events via osascript. Bypasses the Calendar UI. USE THIS for any "加到日历 / schedule X / what's on my calendar?" request — far faster + more reliable than driving the Calendar.app window.
family: bash
requires:
  apps: ['Calendar']
  bins: ['osascript']
---

# Calendar via AppleScript

Calendar's AppleScript dictionary is functional but a little stiff —
follow these patterns to avoid the common pitfalls.

## Create a new event on a named calendar

```bash
osascript <<'EOF'
tell application "Calendar"
  tell calendar "Personal"
    set startDate to current date
    set time of startDate to (15 * hours)        -- 15:00 today
    set endDate to startDate + (1 * hours)
    make new event with properties {
      summary:"Demo OpenSeer",
      start date:startDate,
      end date:endDate,
      location:"Mac"
    }
  end tell
end tell
EOF
```

## Schedule for a specific date

```bash
osascript <<'EOF'
tell application "Calendar"
  set startDate to date "Wednesday, May 7, 2026 at 10:00:00 AM"
  set endDate to startDate + (30 * minutes)
  tell calendar "Personal"
    make new event with properties {summary:"Standup", start date:startDate, end date:endDate}
  end tell
end tell
EOF
```

The `date "..."` literal must use the user's locale format
(`Wednesday, May 7, 2026 at 10:00:00 AM` for US). When in doubt
build dates programmatically from `current date` rather than parsing.

## List today's events from a given calendar

```bash
osascript <<'EOF'
tell application "Calendar"
  tell calendar "Personal"
    set today to current date
    set hours of today to 0
    set minutes of today to 0
    set seconds of today to 0
    set tomorrow to today + 1 * days
    set out to ""
    repeat with e in (events whose start date >= today and start date < tomorrow)
      set out to out & (summary of e) & " @ " & (start date of e) & linefeed
    end repeat
    return out
  end tell
end tell
EOF
```

## Find which calendars exist

```bash
osascript -e 'tell application "Calendar" to get name of every calendar'
```

Run this once if you don't know the calendar name — the user may
have named theirs `"Home"`, `"Work"`, `"Family"`, etc.

## Tips

- Calendar prompts for permission the FIRST time osascript queries
  it. After that it's silent.
- `make new event` requires `start date` AND `end date` — both,
  even for instant events.
- `summary` is the title; `description` is the longer note body.
