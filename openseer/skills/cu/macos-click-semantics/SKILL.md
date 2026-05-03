---
name: macos-click-semantics
description: macOS click behaviour cheatsheet — Stacks, Quick Look, aliases, Smart Folders. The defaults differ from Windows/Linux and from each other.
family: cu
---

# macOS click semantics

Don't reflexively double-click on the desktop. macOS has several icon
types whose click behaviour differs.

## Regular file icon

| Action | Effect |
|---|---|
| single click | select |
| double click | open in default app |
| select + `key space` | **Quick Look** — instant preview overlay (recommended for "show me X") |
| select + `key cmd+o` | open (same as double-click) |
| select + `key enter` | rename (NOT open — common mistake) |

Prefer Quick Look for "show me / preview the X" tasks: it pops up
fast, doesn't change focus to a new app, and dismisses with `esc`.

## macOS Stack (auto-grouped icons)

System Settings → Desktop & Dock → "Use Stacks" is on by default in
recent macOS. When on, files on the desktop auto-group by Kind into
piles labelled "Images", "Documents", "Screenshots", etc. The visible
label is NOT a filename.

| Action | Effect |
|---|---|
| single click | **expand** the stack inline (icons fan out) |
| double click | same as single (does NOT "open the first item") |
| once expanded, single click | select an individual icon |
| once expanded, double click | open the individual icon |

If you see a thumbnail labelled "Images" / "Documents" / "PDFs" with
several overlapping previews — it's a Stack. Single-click first, then
act on the actual file you want.

`open ~/Desktop/Images` will FAIL because no file is literally named
"Images" — Stacks are a Finder display feature, not the filesystem.

## Alias / symlink

Looks like a normal icon with a small arrow badge. Acts like the
target file (double-click opens what it points to). Filename ends in
`.alias` or has no special extension; `ls -l` shows the symlink target.

## Smart Folder (saved Spotlight search)

Looks like a folder with a gear badge. Double-click opens a Finder
window showing live results (it doesn't navigate into a directory on
disk; the contents are query results).

## Folder

Single click selects, double click opens it (or `key cmd+o`,
`key cmd+down`). `key cmd+up` goes back to the parent.

## Quick recipe — "show me the X on Desktop"

1. Bring Finder forward (or `bash open ~/Desktop`).
2. Visually identify the right thumbnail. If it's a Stack, single-click
   to expand first, then identify the specific file.
3. `click <thumbnail>` (single).
4. `key space` → Quick Look preview. Done.

If the user wants the file OPENED (not previewed), step 4 becomes
`key cmd+o` or double-click the now-selected icon.
