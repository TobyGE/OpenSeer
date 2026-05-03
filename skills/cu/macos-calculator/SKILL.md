---
name: macos-calculator
description: Drive macOS Calculator.app by keyboard. No clicking buttons needed.
family: cu
requires:
  apps: ['Calculator']
---

# Calculator — keyboard only

macOS Calculator accepts numeric and operator keys directly. Don't
click number buttons.

Pattern for a fresh computation:

1. Open the app:
   ```json
   {"action": "open_app", "app": "Calculator"}
   ```
2. Wait briefly:
   ```json
   {"action": "wait", "amount": 1}
   ```
3. **Clear stale state** — Calculator persists across launches. Press
   `esc` to wipe the previous session before computing:
   ```json
   {"action": "key", "key": "esc"}
   ```
4. Type the expression in one chain (saves API round-trips):
   ```json
   {"actions": [
     {"action": "key", "key": "1"},
     {"action": "key", "key": "7"},
     {"action": "key", "key": "*"},
     {"action": "key", "key": "4"},
     {"action": "key", "key": "2"},
     {"action": "key", "key": "enter"}
   ]}
   ```
   Or, equivalently, a single `type "17*42="` works.
5. Read the result from the screenshot (top of the Calculator window).
6. To copy the result to the clipboard, `key cmd+c`. To verify, run
   `bash pbpaste`.

Why this matters: clicking Calculator's number/operator buttons via CU
grounding is wasted effort — the buttons are densely packed and mis-grounding
is common. Keyboard input is deterministic.
