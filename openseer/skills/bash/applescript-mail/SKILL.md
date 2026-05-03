---
name: applescript-mail
description: Compose, save as draft, or send Apple Mail messages via osascript.
family: bash
requires:
  apps: ['Mail']
  bins: ['osascript']
---

# Mail via AppleScript

> **Safety note**: Sending mail is irreversible. By default this skill
> prefers `save` (draft) over `send`. Use `send` only when the user
> explicitly says "send" or after explicit confirmation.

## Save a draft (preferred default)

```bash
osascript <<'EOF'
tell application "Mail"
  set newMessage to make new outgoing message with properties {
    subject:"Subject here",
    content:"Body here",
    visible:true
  }
  tell newMessage
    make new to recipient at end of to recipients with properties {address:"a@example.com"}
  end tell
  -- Saved to drafts; user can review before sending
  save newMessage
end tell
EOF
```

Setting `visible:true` opens the compose window so the user can see
the draft. `save` puts it in the Drafts mailbox.

## Send (only when clearly authorized)

Replace `save newMessage` with `send newMessage`. Don't mix them up.

## Multiple recipients / CC / BCC

```bash
osascript <<'EOF'
tell application "Mail"
  set newMessage to make new outgoing message with properties {subject:"...", content:"..."}
  tell newMessage
    make new to recipient at end of to recipients with properties {address:"a@example.com"}
    make new cc recipient at end of cc recipients with properties {address:"cc@example.com"}
    make new bcc recipient at end of bcc recipients with properties {address:"bcc@example.com"}
  end tell
  save newMessage
end tell
EOF
```

## Attachments

```bash
osascript <<'EOF'
tell application "Mail"
  set newMessage to make new outgoing message with properties {subject:"Report", content:"See attached.", visible:true}
  tell newMessage
    make new to recipient at end of to recipients with properties {address:"boss@example.com"}
    make new attachment with properties {file name:(POSIX file "/Users/me/Desktop/report.pdf")} at after the last paragraph
  end tell
  save newMessage
end tell
EOF
```

## List unread inbox count

```bash
osascript -e 'tell application "Mail" to count of (messages of inbox whose read status is false)'
```

## Multi-line bodies (do NOT use `\n`)

AppleScript does not interpret `\n` inside string literals — `"a\nb"`
ends up as the literal 3-char text `a\nb` in the email. Use one of:

- AppleScript concatenation: `"line one" & return & "line two"`
- Shell-side: build the string in bash with real newlines and pass via
  the `on run argv` pattern (shell preserves \n). Example:
  ```bash
  BODY=$'Hi Alice,\n\nReport attached.\n\nBest,\nMe'
  osascript - "$BODY" "Subject" "alice@x.com" <<'AS'
  on run argv
    set theBody to item 1 of argv
    set theSub  to item 2 of argv
    set theTo   to item 3 of argv
    tell application "Mail"
      set m to make new outgoing message with properties {subject:theSub, content:theBody, visible:true}
      tell m to make new to recipient with properties {address:theTo}
      save m
    end tell
  end run
  AS
  ```

## Tips

- `visible:true` is recommended for drafts — gives the user a chance
  to review.
- For rich HTML mail, AppleScript is limited; use `osascript` as the
  launcher and fall back to CU for formatting beyond plain newlines.
