---
name: applescript-system
description: System-level macOS controls via osascript — volume, brightness, notifications, speech, sleep, dialogs, frontmost app.
family: bash
requires:
  bins: ['osascript']
---

# macOS system control via AppleScript

These small one-liners control system-wide state without touching any
app's UI. Useful when the task is "do X to my Mac" rather than "do X
in app Y".

## Volume

```bash
# get
osascript -e 'output volume of (get volume settings)'

# set 0–100
osascript -e 'set volume output volume 50'

# mute / unmute
osascript -e 'set volume output muted true'
osascript -e 'set volume output muted false'
```

## Speech (built-in TTS)

```bash
osascript -e 'say "Hello, world"'
osascript -e 'say "你好" using "Tingting"'
osascript -e 'say "Done." using "Samantha" speaking rate 200'
```

Useful for "tell me when X is done" — kicks a hands-free notification.

## Native notification banner

```bash
osascript -e 'display notification "Build finished" with title "OpenSeer"'
```

## Modal alert / dialog (BLOCKS — only use if you want input)

```bash
# Alert with OK
osascript -e 'display alert "Heads up" message "The doc has been saved."'

# Yes/No dialog
osascript -e 'display dialog "Continue?" buttons {"No","Yes"} default button "Yes"'
```

These pop a modal and block the script until user clicks. Don't use
in long-running automation; the user will be confused.

## Frontmost / focused app

```bash
# What app is in front?
osascript -e 'tell application "System Events" to name of first application process whose frontmost is true'

# Bring an app to front
osascript -e 'tell application "Notes" to activate'

# Minimize all of an app's windows
osascript -e 'tell application "Notes" to set miniaturized of every window to true'
```

## Sleep / wake

```bash
osascript -e 'tell application "Finder" to sleep'    # sleep the Mac
caffeinate -d -t 600 &                                 # prevent sleep for 10 min
```

## Show desktop / Mission Control

```bash
osascript -e 'tell application "System Events" to key code 103'   -- F11 in some setups
osascript -e 'tell application "Mission Control" to launch'
```

(macOS shortcut keys vary by user config; `open -a "Mission Control"`
is more reliable.)

## Tips

- Speech and notifications need NO permissions; volume/brightness do.
- `display notification` is non-blocking — fire-and-forget.
- `display alert` / `display dialog` are MODAL — they pause until
  dismissed. Avoid in unattended scripts.
