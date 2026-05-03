---
name: open-url-or-app
description: Open URLs, files, and applications via the macOS `open` command — far faster than driving the Dock or address bar.
family: bash
requires:
  bins: ['open']
---

# `open` — the universal launcher

`open` ships on every Mac. Use it instead of GUI navigation whenever you
need to start something:

```bash
open https://github.com/TobyGE/OpenSeer    # URL → default browser
open /Users/yingqiang/Desktop/notes.md      # file → default app
open -a Calculator                          # launch named app
open -a "Visual Studio Code" .              # open current dir in VS Code
open -R /path/to/file                       # reveal in Finder (don't open)
```

Notes:
- This is faster, more reliable, and immune to grounding errors versus
  clicking the Dock or typing into Safari's address bar.
- Use it whenever the task says "open <X>", "go to <URL>",
  "show me <file>", or similar.
- The `open_app` action is a shortcut for `open -a <name>`. Use whichever
  feels cleaner.
