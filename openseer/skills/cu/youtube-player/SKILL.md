---
name: youtube-player
description: Drive a focused YouTube video player by keyboard. Chain shortcuts aggressively.
family: cu
---

# YouTube player — keyboard shortcuts

Once a YouTube video has loaded and the player is focused (click once
into the video center), every player control is a single keystroke.

| Key | Action |
|----|--------|
| `space` or `k` | play / pause |
| `m` | mute toggle |
| `j` | seek -10s |
| `l` | seek +10s |
| `,` / `.` | prev / next frame (paused) |
| `0`–`9` | jump to 0%–90% |
| `f` | fullscreen toggle |
| `t` | theatre mode |
| `i` | mini-player |
| `c` | captions |

**Always chain** these in one response. After clicking into the player,
emit a single `actions: [...]` array for the whole sequence:

```json
{"actions": [
  {"action": "click", "x": <player center>, "y": <player center>},
  {"action": "key", "key": "m"},
  {"action": "key", "key": "l"},
  {"action": "key", "key": "l"},
  {"action": "key", "key": "l"},
  {"action": "key", "key": "f"}
]}
```

Don't emit them one-by-one across separate turns — that wastes 5–10s
per round-trip and the keys are deterministic.
