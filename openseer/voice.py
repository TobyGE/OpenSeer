"""Voice loop for the CLI backend.

This module intentionally does not add any computer-use behavior. It only
turns speech into the same task text `openseer task` already accepts, then
speaks the run's final answer.
"""
from __future__ import annotations

import json
import subprocess
import sys
import uuid
from importlib import resources
from pathlib import Path

from .agent import run
from . import auth as auth_mod


def run_voice(args) -> int:
    ok, msg = auth_mod.preflight()
    if not ok:
        print(msg)
        return 1

    listen_once.locale = args.locale
    listen_once.debug = args.debug_voice
    listen_once.allow_server_recognition = args.allow_server_recognition
    print("[voice] backend voice loop", flush=True)
    if args.execute:
        print("[voice] mode: EXECUTE actions", flush=True)
    else:
        print("[voice] mode: DRY-RUN only; add --execute to act on the Mac",
              flush=True)
    print("[voice] speak after the prompt; Ctrl-C to stop", flush=True)
    while True:
        try:
            text = listen_once(args.listen_seconds)
        except KeyboardInterrupt:
            print()
            return 130
        except Exception as e:
            print(f"[voice] listen failed: {e}", file=sys.stderr, flush=True)
            return 1

        if not text:
            print("[voice] heard nothing", flush=True)
            if args.once:
                return 1
            continue
        print(f"[voice] heard: {text}", flush=True)
        if text.strip().lower() in {"quit", "exit", "stop", "退出", "停止"}:
            return 0

        try:
            final = run_task_and_read_final(text, args)
        except KeyboardInterrupt:
            print("\n[voice] stopped", flush=True)
            return 130
        if final:
            print(f"[voice] answer: {final}", flush=True)
            if args.speak:
                speak(final)
        else:
            print("[voice] no final answer found", flush=True)

        if args.once:
            return 0


def listen_once(seconds: float) -> str:
    helper = helper_binary()
    print(f"[voice] listening for {seconds:g}s...", flush=True)
    cmd = [str(helper), "--seconds", str(seconds)]
    if getattr(listen_once, "locale", None):
        cmd.extend(["--locale", listen_once.locale])
    if getattr(listen_once, "debug", False):
        cmd.append("--debug")
    if getattr(listen_once, "allow_server_recognition", False):
        cmd.append("--allow-server-recognition")
    proc = subprocess.run(
        cmd,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if proc.stderr and getattr(listen_once, "debug", False):
        print(proc.stderr.rstrip(), file=sys.stderr, flush=True)
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or f"swift exited {proc.returncode}")
    return proc.stdout.strip()


def helper_binary() -> Path:
    source = Path(str(resources.files("openseer").joinpath("voice_listen.swift")))
    cache_dir = Path.home() / ".openseer" / "voice-helper"
    cache_dir.mkdir(parents=True, exist_ok=True)
    binary = cache_dir / "openseer-voice-listen"
    if (binary.exists()
            and binary.stat().st_mtime >= source.stat().st_mtime):
        return binary
    print("[voice] building local speech helper...", flush=True)
    proc = subprocess.run(
        ["swiftc", "-parse-as-library", str(source), "-o", str(binary)],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or f"swiftc exited {proc.returncode}")
    return binary


def run_task_and_read_final(task: str, args) -> str:
    trace_id = uuid.uuid4().hex[:8]
    out_dir = Path.home() / ".openseer" / "runs" / trace_id
    run(task, max_steps=args.max_steps, dry_run=not args.execute,
        confirm_each=args.confirm_each, sleep_between=args.sleep,
        grounder=args.grounder, external_grounder=args.external_grounder,
        out_dir=out_dir)
    final_path = out_dir / "final.json"
    if not final_path.exists():
        return ""
    try:
        blob = json.loads(final_path.read_text(encoding="utf-8"))
    except Exception:
        return ""
    return (blob.get("last_reason") or blob.get("error") or "").strip()


def speak(text: str) -> None:
    try:
        subprocess.run(["/usr/bin/say", text], check=False)
    except Exception as e:
        print(f"[voice] speak failed: {e}", file=sys.stderr)
