---
name: applescript-notes
description: Create / append / read Apple Notes via osascript. Bypasses CU entirely; far faster and more reliable than driving the Notes window.
family: bash
requires:
  apps: ['Notes']
  bins: ['osascript']
---

# Notes via AppleScript

`osascript` lets you drive Apple Notes by name, with no clicking. Use
this whenever the task involves Notes content — never click into the
Notes window.

## Create a new note

```bash
osascript <<'EOF'
tell application "Notes"
  tell account "iCloud"
    make new note at folder "Notes" with properties {name:"TITLE HERE", body:"BODY HERE"}
  end tell
end tell
EOF
```

The first line of `body` becomes the note's display title in the
sidebar; subsequent lines are the body. **AppleScript string literals
do NOT interpret `\n`** — `"foo\nbar"` becomes the literal 5-character
string `foo\nbar` in the note. To get real newlines:

- Inside an AppleScript literal, concatenate with `return` or `linefeed`:
  `"line one" & linefeed & "line two"`
- Or, better, build the multi-line string in shell first and pass it
  via the `on run argv` pattern below — shell preserves real `\n`.
- Notes body is HTML: `<br>` also works as a line break.

> Note: AppleScript does NOT use shell-style `\` for line continuation.
> Keep `make new note ...` on one logical line, or use AppleScript's
> `¬` (option-L) continuation if it gets long.

## Append to (or replace) an existing note by title

```bash
osascript <<'EOF'
tell application "Notes"
  tell account "iCloud"
    set theNote to first note of folder "Notes" whose name is "EXISTING TITLE"
    set body of theNote to (body of theNote) & "<br>NEW LINE TO APPEND"
  end tell
end tell
EOF
```

Notes' body is HTML; use `<br>` for line breaks, not `\n`.

## List recent notes (titles only)

```bash
osascript <<'EOF'
tell application "Notes"
  tell account "iCloud"
    set out to ""
    repeat with n in (notes of folder "Notes")
      set out to out & (name of n) & linefeed
    end repeat
    return out
  end tell
end tell
EOF
```

## Read a note's body

```bash
osascript -e 'tell application "Notes" to tell account "iCloud" to get body of (first note of folder "Notes" whose name is "TITLE")'
```

Returns HTML. Pipe through `sed 's/<[^>]*>//g'` for plain text.

## Quoting strings with shell variables (argv pattern)

Direct interpolation (`{name:"$TITLE"}`) BREAKS the moment title or
body contains a `"`, `\`, or other AppleScript-special character.
Use the `on run argv` pattern instead — shell handles the quoting,
AppleScript receives properly-typed strings:

```bash
TITLE="Daily log $(date +%F)"
BODY='line one with "quotes"
line two'
osascript - "$TITLE" "$BODY" <<'APPLESCRIPT'
on run argv
  set theTitle to item 1 of argv
  set theBody  to item 2 of argv
  tell application "Notes"
    tell account "iCloud"
      make new note at folder "Notes" with properties {name:theTitle, body:theBody}
    end tell
  end tell
end run
APPLESCRIPT
```

This is the canonical safe pattern — use it whenever the title or body
might contain anything other than plain ASCII.

## Notes the model often gets wrong

- Always include `tell account "iCloud"` (or `"On My Mac"` if iCloud
  isn't enabled). Skipping the account block gives a vague error.
- The folder default is `"Notes"` — different language Macs may have
  it localised. If the script errors with "folder doesn't exist",
  fall back to `default folder`.
- Newlines in `body` for `make new note` use literal `\n` in the
  AppleScript string. For appends to existing notes, use HTML `<br>`.
