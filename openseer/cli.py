"""CLI: python -m openseer.cli_agent "open Calculator and compute 17 * 42"

Default is dry-run (predict + annotate, never click). Pass --execute
to actually drive the keyboard/mouse.
"""
from __future__ import annotations

import argparse

from .agent import run


def main():
    ap = argparse.ArgumentParser(description="GPT-5.5 macOS computer-use agent (basic)")
    ap.add_argument("task", help="Natural-language task description")
    ap.add_argument("--max-steps", type=int, default=10)
    ap.add_argument("--execute", action="store_true",
                    help="Actually execute pyautogui actions (default: dry-run)")
    ap.add_argument("--confirm-each", action="store_true",
                    help="Prompt y/s/q before each action (recommended for first runs)")
    ap.add_argument("--sleep", type=float, default=1.5,
                    help="Seconds to wait between turns (lets UI settle)")
    ap.add_argument("--grounder", default="gpt55", choices=["gpt55", "haiku"],
                    help="Default backend that resolves target descriptions to (x,y)")
    ap.add_argument("--external-grounder", default=None,
                    choices=["gpt55", "haiku"],
                    help="Specialist grounder invoked when model emits "
                         "{action:'reground', external:true}. Defaults to "
                         "same as --grounder (no real escalation).")
    args = ap.parse_args()

    run(args.task, max_steps=args.max_steps, dry_run=not args.execute,
        confirm_each=args.confirm_each, sleep_between=args.sleep,
        grounder=args.grounder, external_grounder=args.external_grounder)


if __name__ == "__main__":
    main()
