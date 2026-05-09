"""OpenSeer command-line interface.

Subcommands:
    openseer task "<task>" [options]    drive the agent loop
    openseer daemon                     run Telegram daemon in this process
    openseer daemon-launcher            launch Terminal-hosted daemon
    openseer auth status                 show ChatGPT OAuth login state
    openseer auth login                  log in via Codex CLI's OAuth flow
    openseer auth logout                 wipe local tokens

For convenience, ``openseer "<task>"`` (no subcommand) is an alias for
``openseer task "<task>"``.
"""
from __future__ import annotations

import argparse
import sys

from . import auth as auth_mod
from .agent import run
from .repl import repl as run_repl
from .setup_wizard import run_setup


# ────────────────────────────  task subcommand  ──────────────────────────────

def _add_task_args(ap: argparse.ArgumentParser) -> None:
    ap.add_argument("task", help="Natural-language task description")
    ap.add_argument("--max-steps", type=int, default=20)
    ap.add_argument("--execute", action="store_true",
                    help="Actually execute pyautogui actions (default: dry-run)")
    ap.add_argument("--confirm-each", action="store_true",
                    help="Prompt y/s/q before each action")
    ap.add_argument("--sleep", type=float, default=0.0,
                    help="Seconds between turns (lets UI settle)")
    ap.add_argument("--grounder", default="gpt55", choices=["gpt55"],
                    help="Default backend that resolves target descriptions to (x,y)")
    ap.add_argument("--external-grounder", default=None,
                    choices=["gpt55"],
                    help="Specialist grounder for reground[external:true]")


def cmd_task(args: argparse.Namespace) -> int:
    # Provider-aware login check before running anything heavy. Older
    # code only checked Codex/ChatGPT OAuth, which rejected users on
    # Anthropic config — they got an "ChatGPT OAuth token" error even
    # though their `provider` was set to anthropic. preflight() checks
    # the right backend.
    ok, msg = auth_mod.preflight()
    if not ok:
        print(msg)
        return 1

    run(args.task, max_steps=args.max_steps, dry_run=not args.execute,
        confirm_each=args.confirm_each, sleep_between=args.sleep,
        grounder=args.grounder, external_grounder=args.external_grounder)
    return 0


# ────────────────────────────  auth subcommands  ─────────────────────────────

def cmd_auth_status(args: argparse.Namespace) -> int:
    st = auth_mod.token_status()
    print(st.summary())
    cli = auth_mod.codex_cli_path()
    print(f"codex CLI:   {cli or 'NOT INSTALLED'}")
    print(f"auth.json:   {auth_mod.AUTH_FILE}")
    return 0 if (st.has_file and not st.expired) else 1


def cmd_auth_login(args: argparse.Namespace) -> int:
    return auth_mod.run_codex_login()


def cmd_auth_logout(args: argparse.Namespace) -> int:
    return auth_mod.run_codex_logout()


# ────────────────────────────  argparse plumbing  ────────────────────────────

def cmd_chat(args: argparse.Namespace) -> int:
    return run_repl()


def cmd_setup(args: argparse.Namespace) -> int:
    return run_setup()


def cmd_daemon(args: argparse.Namespace) -> int:
    from .daemon import run_daemon
    return run_daemon()


def cmd_daemon_launcher(args: argparse.Namespace) -> int:
    from .daemon_launcher import main as launcher_main
    return launcher_main()


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="openseer",
        description="OpenSeer — chat-first, memory-aware macOS computer-use agent",
    )
    sub = ap.add_subparsers(dest="cmd", metavar="{chat,task,daemon,daemon-launcher,auth,setup}")

    p_chat = sub.add_parser("chat", help="Interactive REPL (default if no args)")
    p_chat.set_defaults(func=cmd_chat)

    p_setup = sub.add_parser("setup", help="Guided one-time onboarding")
    p_setup.set_defaults(func=cmd_setup)

    p_task = sub.add_parser("task", help="Run the agent on a one-off task")
    _add_task_args(p_task)
    p_task.set_defaults(func=cmd_task)

    p_daemon = sub.add_parser(
        "daemon",
        help="Listen on inbound channels (Telegram) and run tasks remotely",
    )
    p_daemon.set_defaults(func=cmd_daemon)

    p_daemon_launcher = sub.add_parser(
        "daemon-launcher",
        help="Launch a Terminal-hosted daemon from launchd/watchdogs",
    )
    p_daemon_launcher.set_defaults(func=cmd_daemon_launcher)

    p_auth = sub.add_parser("auth", help="Manage ChatGPT OAuth login")
    sub_auth = p_auth.add_subparsers(dest="auth_cmd", metavar="{status,login,logout}")
    sub_auth.required = True
    sub_auth.add_parser("status", help="Show login state").set_defaults(func=cmd_auth_status)
    sub_auth.add_parser("login",  help="Log in via Codex CLI").set_defaults(func=cmd_auth_login)
    sub_auth.add_parser("logout", help="Wipe local tokens").set_defaults(func=cmd_auth_logout)

    return ap


_KNOWN_SUBCOMMANDS = {"chat", "task", "daemon", "daemon-launcher", "auth", "setup", "-h", "--help"}


def main() -> None:
    argv = sys.argv[1:]

    # `openseer`            → enter REPL
    # `openseer "<task>"`   → one-off task (alias for `openseer task ...`)
    # `openseer task ...`   → explicit one-off
    # `openseer auth ...`   → auth subcommand
    if not argv:
        argv = ["chat"]
    elif argv[0] not in _KNOWN_SUBCOMMANDS and not argv[0].startswith("-"):
        argv = ["task"] + argv

    ap = build_parser()
    args = ap.parse_args(argv)
    if not getattr(args, "func", None):
        ap.print_help()
        sys.exit(2)
    sys.exit(args.func(args) or 0)


if __name__ == "__main__":
    main()
