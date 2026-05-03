---
name: clipboard
description: Read and write the macOS clipboard via `pbcopy`/`pbpaste` — much more reliable than Cmd+C/Cmd+V across app focus changes.
family: bash
requires:
  bins: ['pbcopy', 'pbpaste']
---

# Clipboard via `pbcopy` / `pbpaste`

```bash
pbpaste                       # current clipboard text → stdout
echo "hello" | pbcopy         # set clipboard to "hello"
pbpaste | wc -l               # count clipboard lines
```

Use these instead of `Cmd+C` / `Cmd+V` when:

- you just need the data (model can read `pbpaste` stdout directly).
- you want to verify a copy actually landed in the clipboard.
- you need to push text to the clipboard from a known source instead
  of selecting + copying in some UI.

Combined patterns:

```bash
pbpaste > ~/Desktop/clipboard.txt           # save clipboard to a file
say "$(pbpaste)"                            # speak the clipboard
echo $((1+1)) | pbcopy                      # compute then put result on clipboard
```
