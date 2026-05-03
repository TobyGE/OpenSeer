---
name: find-files
description: Locate files on disk by name / mtime / content — vastly faster than driving Finder Search.
family: bash
requires:
  bins: ['find']
---

# Finding files via `find` / `mdfind` / `grep`

```bash
# files modified in the last day under home, *.md only
find ~ -name '*.md' -mtime -1

# Spotlight metadata search (instant, indexed)
mdfind -onlyin ~ "kMDItemDisplayName == '*notes*'"

# files containing a string, recursive
grep -rln "TODO" ~/Projects --include='*.py'

# count files in a directory
ls ~/Downloads | wc -l
```

When the user asks "find the doc I was editing", "where did I save X",
"list my recent screenshots" — reach for these instead of Finder.
`mdfind` is fastest because it uses Spotlight's index.
