---
name: browser-background
description: Drive a browser via AppleScript + injected JavaScript instead of pyautogui — no mouse / keyboard / focus stealing. Use this when the user asks for "在后台 / background / 别打扰我 / 我要继续用电脑" or when the task is browser-only and would otherwise lock them out for minutes. Works on Chrome, Safari, Arc, Edge.
family: cu
requires:
  apps: ['Google Chrome']
---

# Browser in the background

Lets you do most browser tasks (open URL, click, fill form, read content, navigate, scroll) **without taking over the user's mouse and keyboard**. The trade-off: it can't see the page visually — you read state via the DOM (`document.querySelector`) — so it's not a substitute for pyautogui when you actually need vision (charts, captchas, layout-sensitive UI).

**When to use this**:
- User said "在后台" / "background" / "go do X while I keep working" / similar.
- The task is mostly form-filling, navigation, data extraction.
- You need to run a task for a long time and the user can't sit and watch.

**When NOT to use this**:
- Captchas, visual content (images, charts), pixel-precise clicks → fall back to pyautogui in the foreground.
- Multi-app workflows (need to switch to Calendar.app etc.) → foreground.
- The user is actively using the same browser window for something else — open a NEW window with `--new-window` so you don't clobber their tab.

## Open a dedicated window the agent owns

```bash
open -na "Google Chrome" --args --new-window "<URL>"
```

`-n` forces a new instance (or in single-instance mode, a new window). `--args --new-window` is the Chrome-specific flag. The new window becomes window 1; older user windows shift to window 2+. The user's tabs are untouched.

To find your window id reliably (in case the user opened another window between your `open` and your next call):

```bash
osascript -e 'tell application "Google Chrome" to id of window 1'
```

Save it. All subsequent JS calls target that id explicitly so you never accidentally drive the user's window.

## Run JS in a specific window

```bash
osascript <<'EOF'
tell application "Google Chrome"
  set theWindow to (first window whose id is 12345)   -- replace 12345
  tell active tab of theWindow
    execute javascript "document.title"
  end tell
end tell
EOF
```

This is the workhorse. The JS string is evaluated and its return value is what the `osascript` command prints to stdout.

**Note**: Chrome requires "Allow JavaScript from Apple Events" in View → Developer menu. If the user hasn't enabled it, the first call returns the error `Allow JavaScript from Apple Events`; ask them to enable it via:

```
View menu → Developer → Allow JavaScript from Apple Events
```

(Same setup as `read_page` action.) Safari has the same flag under `Develop` menu.

## Click a button or link

```bash
osascript <<'EOF'
tell application "Google Chrome"
  tell active tab of (first window whose id is 12345)
    execute javascript "
      const el = document.querySelector('button[name=submit]');
      if (!el) { 'NOT_FOUND'; } else { el.click(); 'CLICKED'; }
    "
  end tell
end tell
EOF
```

Selector picking tips:
- Prefer `name` / `id` / `aria-label` attributes; they're stable.
- Avoid raw `nth-child` — the DOM reflows often.
- Use `[data-testid="..."]` if the site has them (React/Vue apps often do).
- If you can't find a stable selector, fall back to text: `Array.from(document.querySelectorAll('button')).find(b => b.textContent.includes('Submit'))?.click()`.

## Fill an input

```javascript
const el = document.querySelector('input[name=email]');
el.focus();
el.value = 'user@example.com';
el.dispatchEvent(new Event('input', { bubbles: true }));
el.dispatchEvent(new Event('change', { bubbles: true }));
```

The `input` + `change` events are needed for React-based sites — setting `.value` alone won't trigger their state. Same script wrapped in `execute javascript` works.

## Read content

```javascript
document.body.innerText                      // all visible text
document.querySelector('main').innerText      // just main
document.querySelectorAll('a').length         // counts
JSON.stringify(Array.from(document.querySelectorAll('h2')).map(h => h.textContent))
// stringify so the AppleScript stdout is a readable single line
```

Long content: limit with `.slice(0, 2000)` so the agent doesn't drown in returned text. If you need more, take a page screenshot via `read_page` (foreground) — but that's the giveaway that you should be in foreground anyway.

## Navigate

```javascript
window.location.href = 'https://example.com/page2';
```

Or via AppleScript directly (cleaner — no JS execution needed):

```applescript
tell application "Google Chrome"
  set URL of active tab of (first window whose id is 12345) to "https://example.com/page2"
end tell
```

After navigation, **WAIT** before the next read — single-page apps re-render asynchronously:

```bash
sleep 1.5    # in your bash action, before the next osascript
```

Or poll for readiness:

```javascript
document.readyState   // 'complete' when load is done
```

## Open / close / list tabs

```applescript
-- list tabs of a window
tell application "Google Chrome"
  set theWindow to (first window whose id is 12345)
  set tabTitles to {}
  repeat with t in tabs of theWindow
    set end of tabTitles to (title of t) & " | " & (URL of t)
  end repeat
  return tabTitles
end tell
```

```applescript
-- open a new tab
tell application "Google Chrome"
  tell (first window whose id is 12345)
    make new tab with properties {URL:"https://example.com"}
  end tell
end tell
```

```applescript
-- close a tab by index
tell application "Google Chrome"
  tell (first window whose id is 12345)
    close tab 3
  end tell
end tell
```

## Footguns observed

- **Window id changes if the user closes/reopens windows.** Re-fetch it (`id of window 1` again) if your stored id stops responding, OR save it once at task start and bail if it disappears (window probably closed).
- **`execute javascript` returns ONLY the last expression's value as a string.** Use `JSON.stringify(...)` for non-trivial outputs.
- **Async JS (await fetch(…) etc.) does NOT work directly** — `execute javascript` is synchronous. Wrap in an IIFE that uses Promises and `then()`, store result in a global, then in the next osascript call read the global. Or just `sleep` between calls.
- **Errors are returned as strings starting with `Error:`** — wrap in a try/catch and return the message rather than letting the script abort.
- **Window doesn't take focus** with this approach — that's the whole point. The user keeps working. But if you accidentally call `window.focus()` or do anything else that activates Chrome (like `tell application "Google Chrome" to activate`), you DO interrupt the user. **Never call `activate` in background mode.**
- **AppleScript can't observe the user's cursor / keyboard** — it can only see DOM state. If a task fails because of a captcha or popup, you have no way to detect that visually without `screencapture`. In that case `ask_user(kind="confirm", attachments=["/tmp/screenshot.png"])` and let the user step in (which then breaks the "background" promise — that's OK, accidents happen).

## Notifying when done

If you do the task fully in background and finish:
```bash
osascript -e 'display notification "OpenSeer done." with title "Background task" sound name "Glass"'
```

The user gets a macOS notification regardless of whether they're looking at Chrome or not.

Don't use `osascript -e 'tell ... to activate'` to "show them the result" — that defeats background mode. Just notify, then `terminate(answer)` with what you found.

## When to escalate to foreground

Detect that you're stuck and need vision / focus: 3 consecutive selector misses, captcha pages (URL contains `recaptcha` / `cf-challenge` / etc.), or any error containing `denied` / `disabled`. Then:

```
ask_user(kind="confirm",
         question="I can't make progress in background. Bring this window to front and let me take over?",
         attachments=["<screencap path>"])
```

If they say yes, `tell application "Google Chrome" to activate window <id>` + switch to pyautogui for the rest.
