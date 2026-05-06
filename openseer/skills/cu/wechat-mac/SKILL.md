---
name: wechat-mac
description: WeChat for Mac UI cheat-sheet — layout, in-chat search, and footguns for tasks that need to read WeChat conversations.
family: cu
requires:
  apps: ['WeChat']
---

# WeChat for Mac UI

Use this only for tasks that involve the actual WeChat conversations.
For sending a message via shortcut you might still drive the GUI; for
"how many unread" you just look at the screen.

## Layout

Three vertical panes, left → right:

1. **Tab rail** (≈60 px wide). Top to bottom: avatar, Chats, Contacts,
   Favorites, Mini Programs, more. Tabs select what fills pane #2.
2. **List** (≈300 px wide). The conversation list when on the Chats
   tab. Search box pinned at the top.
3. **Active chat** (the rest). Header at top with name + a magnifying
   glass icon and a 3-dot menu, then the message thread, then the
   input box at the bottom.

The thread loads incrementally — only the most recent N messages are
visible; older messages load when you scroll up.

## In-chat search (the right way to find messages by date or keyword)

When a chat is open, the chat header has two header controls on the
top right:

- 🔍 **magnifying glass** — opens an in-chat search bar.
  - Accepts keyword OR date inputs. WeChat understands literal dates
    (`2024年5月3日`) and the relative tokens `今天` / `昨天`.
  - This is the fastest way to "view today's messages" in a long
    thread — you do NOT have to scroll up forever.
  - The `type` action handles CJK input via clipboard paste under
    the hood, so you can pass `text:"今天"` directly.
- **⋯ three-dot menu** — opens chat info / settings. Inside there's
  also `查找聊天记录` (search chat history) which is the same thing
  via a different entry point.

Prefer in-chat search over scroll-wheel paging. Scrolling 100+ lines
of messages turn-by-turn is wasteful when one search jumps directly.

## Global search

`cmd+f` (or click the search box at the top of pane #2) searches
across ALL chats. Useful when you have a keyword but don't know which
chat it's in.

## Reading "today's" thread efficiently

1. Open the right chat (click in pane #2, or `cmd+f` to find by name).
2. Click the 🔍 in the chat header.
3. Type `今天`. Pick the first result — it scrolls the thread to
   today's first message.
4. Read down from there. No scroll spam.

## Local database — don't bother

`~/Library/Containers/com.tencent.xinWeChat/Data/Library/Application Support/com.tencent.xinWeChat/<uid>/Message/*.db`
is **AES-encrypted**. `sqlite3` will return `file is not a database`
or unreadable BLOBs. There are third-party decryption tools (chatlog,
WeChatExporter, …) but they require disabling SIP and matching
specific WeChat versions. For ad-hoc agent tasks the GUI is the right
path; don't sink turns into bash on the DB.

## Quitting / closing footguns

- `cmd+w` closes the WINDOW only — WeChat keeps running in the
  background and reopens the same chat next time.
- `cmd+q` actually quits.
- Right-clicking the WeChat dock icon → "Quit" is equivalent to `cmd+q`.

## Privacy note for agent runs

WeChat threads contain personal data. Screenshots written to the
trace directory will include real messages, contact names, and
avatars. Be conservative about what you echo back verbatim into a
`reason` or quote into the model's reasoning if the task is a
summary — paraphrase, don't transcribe.
