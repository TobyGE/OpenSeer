---
name: notion-web
description: Add quick notes / todos / pasted content to Notion via the logged-in browser web app. Use this whenever the user says "加到 Notion" / "save to Notion" / "记到 Notion 里".
family: cu
# `apps` is AND-gated by the skill loader — listing all five
# browsers would require all five installed. Safari ships on every
# Mac, so we gate on it and let the body mention the alternatives.
requires:
  apps: ['Safari']
---

# Notion (web) handoff

For dropping content INTO Notion from anywhere else on the Mac. Assumes the user is logged in to notion.so in their default browser and the target page is reachable.

## Accepted inputs

- Plain text  → appended as a new block in the target page.
- A bullet list  → each line becomes a separate `- ` block.
- A todo / checkbox  → use Notion's `[]` block via `/todo`.
- URL + title  → bookmark block via `/bookmark`.
- A code snippet  → code block via `/code`.

## Where to put it

Read `MEMORY.md` first. The user usually has a default Notion target saved as something like:
```
- notion: default inbox = https://www.notion.so/your-page-id-here
```

If MEMORY.md doesn't have one, ALWAYS `ask_user(kind="text", question="Which Notion page should this go to? Paste the URL.")`. Don't pick at random. After they answer, suggest `write_memory` to save the choice for next time.

## Standard flow

1. Open / focus the page:
   ```
   bash open '<notion url>'
   ```
   `open` uses the user's default browser. If you instead want to ensure a specific browser, `open -a "Google Chrome" '<url>'`.

2. Wait ~1.5s for Notion to settle. Notion's React app re-renders multiple times after navigation; AX/DOM is empty for the first ~800ms.
   ```
   wait amount=2
   ```

3. Refresh AX:
   ```
   get_app_state app="Google Chrome"
   ```
   The Notion editor surfaces as a giant AXTextArea (single role for the whole canvas). Most navigation in the page itself uses keyboard, not click — clicking specific blocks is brittle.

4. Move focus to the END of the page so you don't overwrite existing content:
   ```
   key cmd+end
   ```

5. Insert a new empty line so we don't append to whatever block the cursor landed on:
   ```
   key enter
   ```

6. Now type the content. For:
   - Plain text  →  `type text="..."`. Newlines are literal — Notion treats each newline as a new block.
   - Todo  →  type `/todo` then `enter`, then `type text="..."`. The `/todo` slash command converts the current block to a checkbox.
   - Heading  →  type `/h1` / `/h2` / `/h3` then `enter`, then text.
   - Bullets  →  type `/bullet` then `enter`, then text. Subsequent enters create more bullets.
   - URL bookmark  →  paste the URL on a fresh line, then press `enter`. Notion auto-prompts "Create bookmark" — click it via reground, OR type `/embed` first if you want an embed.

7. Notion autosaves continuously. NO `cmd+s` needed. The save indicator (top-right) reads "Saved" within ~500ms.

## Verifying

`read_page` works to confirm the content landed:
```
read_page selector="main"
```

The text just typed should appear near the bottom of the returned content. If you don't see it, the most likely cause is the focus wasn't in the editor — Notion's left sidebar can steal focus on click. Re-try after `key cmd+end`.

## Footguns observed

- **Sidebar steals focus**: clicking anywhere left of x≈260 lands in the sidebar, not the page. Use keyboard nav (`cmd+end`) instead of click whenever possible.
- **Slash menu blocks `enter`**: after typing `/todo`, the slash menu pops up. Pressing `enter` selects the first menu item (which is what we want). DON'T press escape first or you'll dismiss the menu and `/todo` becomes literal text.
- **`/today` insertion**: Notion's `/today` slash command inserts the date as a mention. Use it for diary-style entries.
- **Inline math via `$$`**: works in Notion. `$$a^2+b^2$$` becomes rendered math. Use sparingly — formatting in inline can break.
- **Pasted rich text**: if the source is HTML (web page selection), `cmd+shift+v` pastes plain text. `cmd+v` keeps formatting which sometimes creates Notion blocks you didn't expect (a whole table from a one-cell selection, etc.). Default to plain unless the user explicitly wants formatting preserved.

## When NOT to use this

- Adding a calendar event → use `applescript-calendar` (native Calendar.app is faster).
- Adding a one-off reminder → use `applescript-reminders` (same).
- Replying to email → use `applescript-mail`.

Notion is for **content the user wants searchable + organized later**, not for time-sensitive single items. If you're not sure, `ask_user(kind="choose", options=["Notion (notes/research)", "Reminders (todo)", "Calendar (event)"])`.
