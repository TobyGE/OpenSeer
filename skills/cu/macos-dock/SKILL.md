---
name: macos-dock
description: Why you should NOT click the macOS Dock to launch apps, and what to do instead.
family: cu
---

# macOS Dock — avoid clicking it

Dock icons are small (~50–60 px), tightly packed, and visually similar.
Even strong vision models miss adjacent icons by 50–150 px. The Dock
also magnifies on hover, which moves icon centres after the first click.

**Do not** try to launch apps by clicking the Dock. Instead:

1. **Preferred** — use the `open_app` action (or `bash` with `open -a`):
   ```json
   {"action": "open_app", "app": "Calculator"}
   ```
   This bypasses the Dock entirely. 100% reliable when the app is installed.

2. **Fallback** — if for some reason `open -a` fails, use Finder:
   `open_app Finder` → `key cmd+shift+a` (Applications folder) →
   `double_click` the app icon (Applications grid icons are large enough
   to ground accurately).

If you've already missed a Dock click once, **stop guessing pixels** and
switch to `open_app`. Don't reground the Dock; the grounder gives the
same kind of error the planner does.
