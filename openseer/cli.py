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
from .setup_wizard import run_setup


# ────────────────────────────  task subcommand  ──────────────────────────────

def _add_task_args(ap: argparse.ArgumentParser) -> None:
    ap.add_argument("task", help="Natural-language task description")
    # 200 matches agent.run's default and the Telegram daemon. The
    # old default of 20 was a leftover debug cap that silently
    # truncated GUI/CLI runs that spawned `openseer task` without
    # explicitly overriding it.
    ap.add_argument("--max-steps", type=int, default=200)
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
    ap.add_argument("--session-context-file", default=None,
                    help="Read prior-conversation context from FILE "
                         "and inject it into the agent's first user "
                         "message. The macOS GUI uses this when "
                         "continuing a thread so the agent knows "
                         "what 'do the same' refers to.")


def cmd_task(args: argparse.Namespace) -> int:
    # Provider-aware login check before running anything heavy.
    ok, msg = auth_mod.preflight()
    if not ok:
        print(msg)
        return 1

    session_context = ""
    if args.session_context_file:
        try:
            from pathlib import Path
            session_context = Path(args.session_context_file
                                   ).read_text(encoding="utf-8")
        except Exception as e:
            print(f"[task] couldn't read session context file: {e}")
            # Don't abort — running without context is still useful.

    # Attach to a running agentd if one's reachable. That way `openseer
    # task` shares the GUI's daemon (warm Python, in-memory caches,
    # same skills loaded once) instead of spinning up a fresh agent
    # process per CLI invocation. Falls back to direct execution if
    # no daemon is running.
    from .agentd_client import try_open, render_event_to_stdout
    client = try_open()
    if client is not None:
        with client:
            print("[cli] attached to running agentd")
            exit_code = 0
            for ev in client.run_task(
                args.task,
                dry_run=not args.execute,
                session_context=session_context,
            ):
                if ev.get("type") == "_ack":
                    print(f"[cli] run_id: {ev.get('run_id')}")
                    continue
                render_event_to_stdout(ev)
                et = ev.get("type")
                if et == "task_finished":
                    status = (ev.get("data") or {}).get("status", "done")
                    exit_code = 0 if status == "done" else 1
                elif et == "task_failed":
                    exit_code = 2
            return exit_code

    # Fallback: direct in-process agent.run(). Same path as before
    # the agentd refactor; some flags (confirm_each, sleep,
    # grounder, external_grounder) only work in this path right
    # now — agentd doesn't yet thread them through.
    from .agent import run
    run(args.task, max_steps=args.max_steps, dry_run=not args.execute,
        confirm_each=args.confirm_each, sleep_between=args.sleep,
        grounder=args.grounder, external_grounder=args.external_grounder,
        session_context=session_context)
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
    # `--provider` lets the GUI's setup wizard trigger the right
    # OAuth browser flow without each side reimplementing the
    # shell-out. Default stays openai (legacy callers).
    provider = (getattr(args, "provider", None) or "openai").lower()
    if provider == "anthropic":
        if getattr(args, "legacy", False):
            from .setup_wizard import run_claude_login
            return run_claude_login()
        from . import oauth_anthropic
        # GUI uses --start / --finish so it can render the paste-
        # back step natively. Plain `--provider anthropic` (no
        # mode) keeps the interactive CLI flow.
        mode = getattr(args, "mode", None)
        if mode == "start":
            return oauth_anthropic.run_login_start()
        if mode == "finish":
            # `--code` / `--verifier` were the original argv-based
            # interface; they survive only for CLI testing where
            # passing through `ps` is acceptable. The GUI now uses
            # the stdin form so other local processes can't read
            # the OAuth code/verifier from /proc-style argv views
            # (codex P2).
            if args.code and args.verifier:
                return oauth_anthropic.run_login_finish(
                    code=args.code, verifier=args.verifier,
                    expected_state=args.state or None,
                )
            return oauth_anthropic.run_login_finish_from_stdin()
        return oauth_anthropic.run_login()
    # OpenAI: run the OAuth flow directly so users don't need the
    # Codex CLI installed. Falls through to codex CLI only if our
    # native flow can't bind its callback port (busy 1455) and the
    # user explicitly wants the legacy path via `--legacy`.
    if getattr(args, "legacy", False):
        return auth_mod.run_codex_login()
    from . import oauth_openai
    return oauth_openai.run_login()


def cmd_auth_logout(args: argparse.Namespace) -> int:
    return auth_mod.run_codex_logout()


# ────────────────────────────  argparse plumbing  ────────────────────────────

def cmd_chat(args: argparse.Namespace) -> int:
    from .repl import repl as run_repl
    return run_repl()


def cmd_check(args: argparse.Namespace) -> int:
    """Print system status: provider logins, TCC permissions, Telegram
    config. Used by the macOS GUI's setup wizard (calls with --json)
    and as a quick CLI diagnostic for users."""
    from . import check as _check
    return _check.main(json_out=bool(args.json))


def cmd_reset(args: argparse.Namespace) -> int:
    """Factory-reset: wipe OAuth tokens, configs, TCC grants. Used
    by the GUI's "Re-run setup" button."""
    from . import reset as _reset
    return _reset.run()


def cmd_permissions_request(args: argparse.Namespace) -> int:
    """Trigger the macOS Accessibility + Screen-Recording prompts
    FROM THIS python process so it ends up in the relevant Privacy
    lists. The GUI's setup wizard calls this when the user clicks
    Request — without it, the python child never appears in System
    Settings and capture/control silently fail at runtime."""
    from . import check as _check
    return _check.request_permissions()


def cmd_setup(args: argparse.Namespace) -> int:
    return run_setup()


def cmd_daemon(args: argparse.Namespace) -> int:
    from .daemon import run_daemon
    return run_daemon()


def cmd_agentd(args: argparse.Namespace) -> int:
    from .agentd import run_agentd
    return run_agentd()


def cmd_daemon_launcher(args: argparse.Namespace) -> int:
    from .daemon_launcher import main as launcher_main
    return launcher_main()


def cmd_voice(args: argparse.Namespace) -> int:
    from .voice import run_voice
    return run_voice(args)


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="openseer",
        description="OpenSeer — chat-first, memory-aware macOS computer-use agent",
    )
    sub = ap.add_subparsers(dest="cmd", metavar="{chat,task,voice,daemon,daemon-launcher,auth,setup}")

    p_chat = sub.add_parser("chat", help="Interactive REPL (default if no args)")
    p_chat.set_defaults(func=cmd_chat)

    p_setup = sub.add_parser("setup", help="Guided one-time onboarding")
    p_setup.set_defaults(func=cmd_setup)

    p_check = sub.add_parser(
        "check",
        help="Print system status (auth, permissions, telegram). Add --json for the GUI.",
    )
    p_check.add_argument("--json", action="store_true",
                         help="Emit a JSON blob instead of human summary")
    p_check.set_defaults(func=cmd_check)

    sub.add_parser(
        "reset",
        help="Factory-reset: wipe OAuth tokens, configs, TCC grants",
    ).set_defaults(func=cmd_reset)

    p_perm = sub.add_parser(
        "permissions",
        help="Request TCC prompts (Accessibility, Screen Recording) "
             "from THIS python process so macOS adds it to the "
             "Privacy lists.",
    )
    sub_perm = p_perm.add_subparsers(dest="perm_cmd", required=True)
    sub_perm.add_parser("request",
                        help="Trigger the prompts and exit"
                        ).set_defaults(func=cmd_permissions_request)

    p_task = sub.add_parser("task", help="Run the agent on a one-off task")
    _add_task_args(p_task)
    p_task.set_defaults(func=cmd_task)

    p_voice = sub.add_parser(
        "voice",
        help="Backend voice loop: listen, run a normal task, speak the final answer",
    )
    p_voice.add_argument("--once", action="store_true",
                         help="Listen/run/speak once, then exit")
    p_voice.add_argument("--listen-seconds", type=float, default=8.0,
                         help="Seconds to record for each utterance")
    p_voice.add_argument("--speak", action="store_true",
                         help="Speak the final answer with macOS `say`")
    p_voice.add_argument("--no-speak", action="store_true",
                         help=argparse.SUPPRESS)
    p_voice.add_argument("--locale", default=None,
                         help="Speech recognition locale, e.g. zh-CN or en-US")
    p_voice.add_argument("--debug-voice", action="store_true",
                         help="Print partial transcripts from the speech helper")
    p_voice.add_argument("--allow-server-recognition", action="store_true",
                         help="Allow Apple's server-based Speech recognition "
                              "when an on-device model is unavailable")
    _add_task_args(p_voice)
    # Voice supplies the task from speech, not argv.
    for action in p_voice._actions:
        if action.dest == "task":
            action.nargs = "?"
            action.default = ""
            action.help = argparse.SUPPRESS
            break
    p_voice.set_defaults(func=cmd_voice)

    p_daemon = sub.add_parser(
        "daemon",
        help="Listen on inbound channels (Telegram) and run tasks remotely",
    )
    p_daemon.set_defaults(func=cmd_daemon)

    p_agentd = sub.add_parser(
        "agentd",
        help="WebSocket daemon (Phase 1 skeleton). One process, many clients.",
    )
    p_agentd.set_defaults(func=cmd_agentd)

    p_daemon_launcher = sub.add_parser(
        "daemon-launcher",
        help="Launch a Terminal-hosted daemon from launchd/watchdogs",
    )
    p_daemon_launcher.set_defaults(func=cmd_daemon_launcher)

    p_auth = sub.add_parser("auth", help="Manage ChatGPT OAuth login")
    sub_auth = p_auth.add_subparsers(dest="auth_cmd", metavar="{status,login,logout}")
    sub_auth.required = True
    sub_auth.add_parser("status", help="Show login state").set_defaults(func=cmd_auth_status)
    p_login = sub_auth.add_parser(
        "login",
        help="Run the OAuth browser flow for the selected provider",
    )
    p_login.add_argument(
        "--provider", choices=["openai", "anthropic"], default="openai",
        help="openai → ChatGPT OAuth (default); anthropic → Claude OAuth",
    )
    p_login.add_argument(
        "--legacy", action="store_true",
        help="(openai only) shell out to `codex login` instead of "
             "the built-in OAuth flow. Requires the Codex CLI.",
    )
    # Two-step Anthropic flow for GUI consumers. CLI users can ignore
    # these — bare `--provider anthropic` does the interactive flow.
    p_login.add_argument(
        "--mode", choices=["start", "finish"],
        help="(anthropic only) `start` opens the browser and prints "
             "{state,verifier,url} on stdout; `finish` exchanges a "
             "user-pasted code for tokens.",
    )
    p_login.add_argument("--code", help="(anthropic --mode=finish) "
                                       "code from the success page")
    p_login.add_argument("--verifier", help="(anthropic --mode=finish) "
                                           "code_verifier from --mode=start")
    p_login.add_argument("--state", help="(anthropic --mode=finish, "
                                        "optional) expected state for "
                                        "CSRF check")
    p_login.set_defaults(func=cmd_auth_login)
    sub_auth.add_parser("logout", help="Wipe local tokens").set_defaults(func=cmd_auth_logout)

    return ap


_KNOWN_SUBCOMMANDS = {"chat", "task", "voice", "daemon", "daemon-launcher", "agentd", "auth", "setup", "check", "permissions", "reset", "-h", "--help"}


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
