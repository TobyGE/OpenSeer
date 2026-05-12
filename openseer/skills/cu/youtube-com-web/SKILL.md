---
name: youtube-com-web
description: Summarize what a YouTube video is about WITHOUT opening it in a player. Use this when the user says "总结这个视频" / "这个视频讲了什么" / "summarize this YouTube video" with a youtube.com/watch URL (or a youtu.be short link) — current page URL, clipboard, or task text.
family: cu
requires:
  # Gate on Safari (ships with every Mac) so this skill is always
  # available. The body uses curl rather than the browser anyway —
  # the apps list is just availability metadata.
  apps: ['Safari']
---

# YouTube video summary (web)

For "what's this video about" / "总结一下这个视频" style requests on a
youtube.com or youtu.be URL. The point of this skill is to skip ~3
wasted detour steps that otherwise happen every time.

## What NOT to do

- **Do not call `yt-dlp`.** It is frequently not installed on the user's
  Mac and the open_app / brew install detour wastes 1–2 steps.
- **Do not download the caption track from the `timedtext` API even if
  the page advertises one.** In practice the XML form returns an empty
  body (`xml.etree ParseError: no element found: line 1, column 0`) and
  the `fmt=json3` form returns 0 bytes too. Treating the advertised
  caption track as success will cost you 2 retry steps; assume it
  doesn't work.
- Do not open the video in a browser tab unless the user explicitly
  wants to watch it — the agent doesn't watch video.

## What works (one shot)

Fetch the page HTML and extract `shortDescription` + the chapter
timestamps that creators put in the description. That's enough to
answer "what's this video about" for 95% of cases — the timestamps
function as a free table of contents.

When you send this as a `bash` action, paste the full snippet AS-IS
and replace `__URL__` on the first python line with the actual video
URL. The agent's bash runner has no `$1` to inherit, so positional
argv-style `URL="$1"` won't work; embedding the URL into the python
literal sidesteps that.

```bash
python3 - <<'PY'
import urllib.request, re, html, json
URL = "__URL__"   # ← https://www.youtube.com/watch?v=... or youtu.be/...
req = urllib.request.Request(URL, headers={"User-Agent": "Mozilla/5.0"})
raw = urllib.request.urlopen(req, timeout=20).read().decode("utf-8", "replace")

# Title — most reliable from <title>...</title>; ytInitialData
# carries a 'title' too but its quoting is hairy.
t = re.search(r"<title>(.*?)</title>", raw, re.S)
title = html.unescape((t.group(1) if t else "").replace(" - YouTube", "").strip())

# Description — the player response embeds the full description as a
# JSON-encoded string. Decoding with json.loads on a re-quoted slice
# is the only correct way to unescape it: it handles \\n, \\u4e2d for
# CJK characters, escaped quotes, surrogate pairs, etc. The previous
# attempt (`.encode('utf-8').decode('unicode_escape')`) corrupts
# every non-ASCII byte by re-decoding already-UTF-8 bytes as latin1.
m = re.search(r'"shortDescription":"((?:[^"\\]|\\.)*)"', raw)
desc = json.loads('"' + m.group(1) + '"') if m else ""

print("TITLE:", title)
print("---")
print(desc[:4000])
PY
```

Then summarize from TITLE + first ~4k chars of description (which
typically contains author intent + chapter list).

## Footguns

- youtu.be short links 302-redirect to /watch?v=… — `curl -L` follows.
- Some videos (age-gated, premiered) return a different player
  response without `shortDescription`. In that fallback, `description`
  inside `videoDetails` (look for `"description":{"simpleText":"..."}`)
  works.
- The user may pass the URL via the clipboard rather than as task text;
  if no URL is in the task, `pbpaste` first.

## When to update vs use this skill

Skill is for the "agent doesn't watch the video, just summarizes" path.
If the user explicitly says "play this for me" or "go to timestamp X",
that's a different task — open Chrome / Safari to the URL instead.
