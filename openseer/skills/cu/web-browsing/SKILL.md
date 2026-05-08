---
name: web-browsing
description: General macOS browser playbook — scrolling, finding text, URL bar, tab management, and per-browser footguns. Use when the target is a webpage. Covers Safari, Chrome, Firefox, and Arc.
family: cu
requires:
  apps: ['Safari']
---

# macOS browser playbook

Read this when the target is inside a webpage and AX-tree elements
alone aren't enough — i.e. you need to scroll, find text by keyword,
switch tabs, or otherwise drive the browser chrome.

The single biggest CU mistake on web pages is **searching the AX
dump for an element that isn't in the viewport** and then giving up.
The element may be 5 screens down. Scroll or use find — see below.

## Strategy first

**The single most important action for web tasks: `read_page`.**

Before scroll-spamming, ask: do I need to read the *content* of this
page (posts, an article, search results, a thread), or do I need to
*click* something on it (a button, a tab)?

- **Reading content** → `read_page`. One call dumps the active tab's
  full text (title + URL + ~8k chars of innerText). Works on SPAs
  (X / LinkedIn / Substack / etc.) where `web_fetch`/`curl` returns
  empty shells. Pass `selector` to grab a specific element only:
  `selector="article"` for a single post, `selector="main"` for the
  primary content region. Combine with `url=...` to navigate first
  in one shot. This replaces 5–10 scroll-and-screenshot turns.
- **Clicking something** → keep using AX index / `click(x,y)` /
  `cmd+F` to find the element, then click it.

When you need to locate something on a page (no `read_page` first):

1. **Looking for specific text** → `cmd+F` (works in Safari, Chrome,
   Firefox, Arc). Type a few unique words, press `enter`. The page
   scrolls to the match. `cmd+G` next match, `cmd+shift+G` previous.
2. **Skimming a long page for structure** → `read_page` first. Only
   fall back to `space` / `shift+space` viewport scrolling if you
   need to see *visual* layout (charts, images, video controls).
3. **Looking for a heading/landmark** → `read_page` and grep the
   text yourself; or `cmd+F` with a likely word.
4. **Static page, no JS** → `web_fetch` / `bash curl -L <url>` are
   fine and faster (no browser activation, no AppleScript).

After any scroll/find action, the layout changes — break the chain
and look at the new screenshot/AX tree before deciding next.

### read_page prerequisites

`read_page` runs JavaScript via AppleScript. The user must enable
"Allow JavaScript from Apple Events" once per browser:
- **Chrome / Arc / Edge / Brave**: View menu → Developer →
  "Allow JavaScript from Apple Events"
- **Safari**: Settings → Advanced → enable "Show features for web
  developers" first, then Develop menu → "Allow JavaScript from
  Apple Events"

If `read_page` returns an "ERROR: … refused JS injection" message,
the user hasn't enabled it. Surface the menu path to them in your
terminate.reason and skip to fallback strategies (`cmd+F`, scroll +
screenshot, `web_fetch`).

## Verified shortcuts (Safari / Chrome / Firefox / Arc)

These are confirmed against each browser's official docs unless
flagged otherwise.

### Find / page navigation

- `cmd+F` — find on page (all four).
- `cmd+G` / `cmd+shift+G` — find next / previous match. Confirmed in
  Chrome and Firefox official docs; standard macOS find convention
  in Safari and Arc.
- `space` / `shift+space` — page down / up (all four; Chrome quotes
  it explicitly).
- `pageup` / `pagedown` keys — work everywhere.
- `cmd+up` / `cmd+down` for top/bottom of page is a macOS-wide
  convention but is **not enumerated in any browser's official
  shortcut doc**. Treat as "try it, fall back to repeated `space`".

### URL / address bar

- Safari / Chrome / Firefox: `cmd+L` focuses the URL bar.
- **Arc has no separate URL bar.** `cmd+L` and `cmd+T` both open
  Arc's **Command Bar** (the unified URL + search + new-tab + command
  palette). Type your URL/query and press `enter` to navigate or
  open a new tab. Don't expect a separate "new tab" key.

### Tabs

- `cmd+T` — new tab (Safari/Chrome/Firefox). **In Arc, `cmd+T` opens
  the Command Bar; the actual new-tab happens when you press `enter`
  with a URL/query.**
- `cmd+W` — close current tab (all four). In Arc this *archives* the
  tab rather than fully discarding it.
- `cmd+shift+T` — reopen last closed tab in Safari/Chrome/Firefox.
  **Arc behaves differently** (closing archives the tab); to recover
  a closed tab in Arc, open the Command Bar and search for "Open
  Recently Closed" or look in Archive.
- `cmd+1` … `cmd+8` jump to tabs 1–8, `cmd+9` jumps to the **last**
  tab. Confirmed in Safari/Chrome/Firefox. **In Arc, `cmd+1`–`cmd+9`
  map to *pinned tabs / Favorites in the sidebar*, not to the Nth
  open tab in any strip.**
- `cmd+option+left` / `cmd+option+right` — cycle tabs (Chrome,
  Firefox; Safari additionally supports `ctrl+tab` and
  `cmd+shift+]` / `cmd+shift+[`).

### Reload

- `cmd+R` — reload (all four).
- **TRAP: `cmd+shift+R`**:
  - Safari: opens **Reader Mode**, NOT hard reload.
  - Chrome / Firefox / Arc (Chromium): hard reload, bypassing cache.
  - Don't use `cmd+shift+R` to "force a refresh" if you're in Safari
    — you'll just toggle Reader. Use `cmd+option+R` (Develop menu
    Empty Caches and Reload) only if Develop menu is enabled.

### Back / forward / history

- `cmd+[` back, `cmd+]` forward — confirmed Safari, Chrome, Firefox;
  standard in Arc.
- **History**: `cmd+y` opens the History page in Chrome (confirmed).
  Safari historically uses `cmd+y` for "Show All History" but it's
  not on Apple's current shortcut page.
  **Firefox on macOS: `cmd+y` is NOT history — it's Downloads.** Use
  `cmd+shift+h` for the History sidebar in Firefox, or `cmd+shift+o`
  for the Library.
- Arc: open the Command Bar, type "history".

### Zoom

- `cmd+=` / `cmd+-` zoom in / out (all four; the `+` key is reached
  via `cmd+=` on a US keyboard, no shift needed for most browsers).
- `cmd+0` reset to 100% (Chrome, Firefox; works in Safari but isn't
  enumerated on Apple's shortcut page).

### Reader / clean view

- Safari: `cmd+shift+R` toggles Reader Mode (when available).
- Firefox: `cmd+option+R` toggles Reader View.
- Chrome and Arc: no native reader-mode keyboard shortcut.

### Tab search / overview

- Chrome: `cmd+shift+a` opens the tab search dropdown (confirmed).
- Safari: `cmd+shift+\` opens **tab overview** (visual grid; you can
  type to filter once it's open). Confirmed via Apple's docs.
- Firefox: no documented tab-search shortcut.
- Arc: tab search is part of the Command Bar (`cmd+t`).

### Bookmarks

- `cmd+d` adds a bookmark (Chrome / Firefox confirmed; standard in
  Safari). **In Arc, `cmd+d` *pins* the current tab to the sidebar
  (Arc's analog of bookmarks).**
- Show bookmarks bar: `cmd+shift+b` in **Chrome and Firefox**.
  Safari uses different navigation (`ctrl+cmd+1` for sidebar);
  `cmd+shift+b` is NOT the Safari bookmarks-bar shortcut.

### DevTools

- Chrome / Firefox / Arc: `cmd+option+i` opens DevTools.
- **Safari**: Web Inspector requires the Develop menu to be enabled
  first (Settings → Advanced → "Show features for web developers").
  Once enabled, `cmd+option+i` opens it. If it fails, the Develop
  menu probably isn't on — don't keep retrying the shortcut.

## Arc-specific notes (because Arc is not Chrome-with-a-different-coat)

- The single most important Arc fact: **`cmd+T` and `cmd+L` both
  open the Command Bar**, the unified entry point for URL / search /
  command palette. Always follow with typing the URL/query and
  pressing `enter`. There is no separate location bar.
- Tabs live in a **left sidebar**, not a top strip. The sidebar is
  divided into **Favorites** (pinned, persistent) and **Today's
  tabs** (auto-archived after 12 hours by default).
- `cmd+1`–`cmd+9` jump to **Favorites slots in the sidebar**, not to
  arbitrary open tabs.
- `cmd+W` archives the current tab. To literally close-without-trace
  isn't standard; archived tabs are recoverable via the Archive.
- `cmd+s` toggles the sidebar visibility. If your screenshots
  suddenly show no tab list, the sidebar may just be hidden.
- The "Boost" feature lets users inject custom CSS/JS per site —
  occasionally a page will look unlike its vanilla rendering. Don't
  treat unfamiliar styling as a different page.

## When AX is sparse on a webpage

Browser AX trees are uneven:

- **Safari** generally exposes a reasonably complete DOM-derived AX
  tree.
- **Chrome / Arc / Edge** (Chromium): AX is exposed but can be flat
  — large regions appear as a single "AXGroup" without per-element
  drill-in. Don't assume AX missed an element; the element may not
  be exposed at the AX layer at all.
- **Firefox**: AX integration on macOS is partial; expect gaps.

If AX returns very few elements for a clearly busy page:

1. Try `cmd+F` and type a few words from what you can see in the
   screenshot — let the browser jump to the element you want,
   then click on it via screenshot coordinates.
2. Or `reground` with a region zoom on the area of interest — the
   visual grounder may resolve where AX failed.
3. Or extract via `bash curl -L "<url>"` and parse — fastest for
   static content; useless for JS-rendered SPAs.

## Common page footguns

- **Cookie / GDPR banners** can sit above the real content and steal
  the click target. If your click misses, check if a banner appeared.
- **Login walls / paywalls** intercept navigation invisibly.
  Verify the URL after a click matches what you expected.
- **Infinite scroll** never gets to a `cmd+down` "bottom of page" —
  the page extends as you scroll. Don't rely on bottom-of-page
  detection; use `cmd+F` for known text instead.
- **Single-page apps** route via JS without changing the URL on
  every nav. The URL bar may lag behind the visible state.

## Anti-patterns

- Scrolling 20 turns to find a sentence you could have located in
  one `cmd+F`.
- Treating an empty AX dump as "page is empty" — it usually means
  the AX tree is sparse for this browser, not that the page has no
  content. Look at the screenshot.
- Using `cmd+shift+R` in Safari expecting a hard reload (you'll
  toggle Reader Mode instead).
- Relying on `cmd+T` "definitely opens a new blank tab" in Arc.

## Confirming before hard-to-reverse actions

Web tasks frequently end at a payment / submission / publish button.
Don't click it. Stop one step earlier and confirm with `ask_user`
when it's available, attaching a screenshot of the current screen
so the user sees exactly what's about to happen.

Trigger list — confirm before:

- ANY payment / "Place Order" / "Confirm Purchase" button (AMC,
  Amazon, Costco, Instacart, etc.).
- Posting a tweet / X reply / LinkedIn post / Reddit comment.
- Sending a chat message in WeChat / Slack / iMessage web.
- Deleting an item, archiving a chat, unsubscribing from a paid
  service.
- Submitting any form whose result is hard to undo (visa application,
  appointment booking, account closure).

Pattern:

```
1. bash screencapture -x /tmp/openseer-confirm.png
2. ask_user kind=confirm question="Place this order? Total $39.96,
   2 Adult tickets, AMC Stony Brook 17, Sat May 9 7:30 PM, seats G3+G4."
   attachments=["/tmp/openseer-confirm.png"]
3. read the reply on next turn:
   - "Yes" → click the final button → terminate(done) verifying it
   - "No"  → terminate(fail) with reason "user declined"
```

Don't confirm trivial intermediate clicks (Continue / Get Tickets /
Next page navigation). Confirm only the final commit step where
backing out costs the user real money or social capital.

## What MEMORY.md skips vs what it does NOT skip

MEMORY.md cached preferences let you skip **choice / preference
asks**, NOT the **final commit confirmation**. The "Confirming
before hard-to-reverse actions" rule above is non-negotiable: even
when every preference is cached, the very last click that costs the
user real money / sends a public message / submits a form STILL
needs an `ask_user(kind="confirm")` with a screenshot.

Concrete distinction:

| Cached in MEMORY.md | Effect |
|---|---|
| `payment: AMEX 1234 (default)` | Skip "which card?" — auto-select AMEX 1234 in the card picker. **Still confirm before clicking Place Order.** |
| `shipping: Palo Alto, CA 94025` | Skip "which address?" — pick the matching saved address. Still confirm the final order. |
| `seats: rear row, center` | Skip "which seat type?" — auto-pick rear-center seats. Still confirm before purchase. |

Only ask preference questions when MEMORY.md doesn't cover them OR
when the on-screen options don't match what's cached.

## Memory-first lookup → narrowed ask

When MEMORY.md PARTIALLY answers (e.g. has payment but not address),
phrase the ask narrowly:

```
ask_user kind=confirm question="Use the cached AMEX 1234 and ship to
[address from form prefill]?"
```

vs. starting from scratch with a broad open-ended ask. Less typing
for the user, faster path to action.
