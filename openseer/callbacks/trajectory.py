"""Persists the full trajectory of a run to disk: per-step screenshots,
prompts, raw responses, SSE events, plus task.json header, events.jsonl
firehose, final.json footer, transcript.json (machine), trace.md (human).
"""
from __future__ import annotations

import json
import os
import shutil
import time
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any

from .base import Callback


_RUN_RETENTION_DAYS_ENV = "OPENSEER_RUN_RETENTION_DAYS"
_DEFAULT_RUN_RETENTION_DAYS = 7


def _run_timestamp(run_dir: Path) -> float:
    """Best-effort timestamp for retention pruning.

    Prefer trace metadata because directory mtimes can change when
    auxiliary files are written later. Fall back to filesystem mtime for
    legacy or partial traces.
    """
    for name, key in (("final.json", "ended_at"), ("task.json", "started_at")):
        path = run_dir / name
        if not path.exists():
            continue
        try:
            val = json.loads(path.read_text()).get(key)
            if isinstance(val, (int, float)):
                return float(val)
        except Exception:
            pass
    return run_dir.stat().st_mtime


def _retention_days() -> int | None:
    raw = os.environ.get(_RUN_RETENTION_DAYS_ENV)
    if raw is None or raw.strip() == "":
        return _DEFAULT_RUN_RETENTION_DAYS
    try:
        days = int(raw)
    except ValueError:
        return _DEFAULT_RUN_RETENTION_DAYS
    return days if days > 0 else None


def _prune_old_runs(runs_root: Path, *, keep: Path, now: float) -> None:
    days = _retention_days()
    if days is None or runs_root.name != "runs" or not runs_root.exists():
        return
    cutoff = now - (days * 24 * 60 * 60)
    keep = keep.resolve()
    for child in runs_root.iterdir():
        if child.name == "latest" or child.is_symlink() or not child.is_dir():
            continue
        try:
            if child.resolve() == keep:
                continue
            if _run_timestamp(child) >= cutoff:
                continue
            shutil.rmtree(child)
        except OSError:
            pass


def _redact_input_for_log(input_items: list[dict]) -> list[dict]:
    """Replace base64 image data URLs with a short placeholder so saved
    JSON is human-readable. Originals stay in step{N}-raw.png on disk.
    """
    out = []
    for it in input_items:
        new_content = []
        for c in it["content"]:
            if c.get("type") == "input_image":
                url = c.get("image_url", "")
                head = url[:40] if isinstance(url, str) else "<obj>"
                new_content.append({
                    "type": "input_image",
                    "image_url": f"<base64 image, {len(url) if isinstance(url, str) else 0} chars, head={head}…>",
                })
            else:
                new_content.append(c)
        out.append({"role": it["role"], "content": new_content})
    return out


class TrajectoryCallback(Callback):
    """Writes:
      out_dir/task.json               (header — task, model, trace_id, started_at)
      out_dir/system_prompt.txt
      out_dir/events.jsonl            (every TaskEvent in order — replayable)
      out_dir/step{N}-raw.png         (set by agent before this hook)
      out_dir/step{N}-action.png      (set by agent)
      out_dir/step{N}-input.json      (we write)
      out_dir/step{N}-response.txt    (we write)
      out_dir/step{N}-events.jsonl    (SSE events from the model call)
      out_dir/final.json              (footer — final_status, totals, n_steps)
      out_dir/transcript.json         (machine-readable full trace)
      out_dir/trace.md                (human-readable Markdown)

    Also maintains ~/.openseer/runs/latest as a symlink to the most
    recent trace_id directory so `/show last` is O(1).
    """

    def __init__(self, verbose: bool = True):
        self.verbose = verbose
        self._events_fh = None        # open file handle for events.jsonl
        self._failed_error: str | None = None    # set if TASK_FAILED fires

    def on_run_start(self, ctx: dict[str, Any]) -> None:
        # Reset per-run state so a callback instance reused across
        # multiple run() calls doesn't leak failure status from a
        # previous run into the current one's final.json.
        self._failed_error = None
        out_dir: Path = ctx["out_dir"]
        _prune_old_runs(out_dir.parent, keep=out_dir, now=time.time())
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "system_prompt.txt").write_text(ctx["system_prompt"])
        (out_dir / "task.json").write_text(json.dumps({
            "task": ctx["task"],
            "model": ctx.get("model"),
            "trace_id": ctx.get("trace_id"),
            "max_steps": ctx.get("max_steps"),
            "dry_run": ctx.get("dry_run"),
            "started_at": ctx.get("started_at", time.time()),
        }, indent=2, ensure_ascii=False))
        # Open events.jsonl for streaming writes; closed in on_run_end.
        self._events_fh = (out_dir / "events.jsonl").open("w")
        # Update ~/.openseer/runs/latest symlink
        runs_root = out_dir.parent
        if runs_root.name == "runs":
            latest = runs_root / "latest"
            try:
                if latest.is_symlink() or latest.exists():
                    latest.unlink()
                latest.symlink_to(out_dir.name)  # relative symlink → trace_id
            except OSError:
                pass    # symlink creation can fail in odd filesystems; non-fatal

    def on_event(self, ctx: dict[str, Any], event: Any) -> None:
        """Stream every TaskEvent to events.jsonl as it happens."""
        # Capture failure so on_run_end can write the right status into
        # final.json — without this, /history shows model errors as `cap`
        # or `empty` because no terminal action was recorded.
        if event.type == "task_failed":
            self._failed_error = str(event.get("error", "unknown error"))
        if self._events_fh is None:
            return
        try:
            payload = {
                "type": event.type,
                "ts": event.timestamp,
                "step": event.step,
                "data": event.data,
            }
            self._events_fh.write(json.dumps(payload, ensure_ascii=False, default=str) + "\n")
            self._events_fh.flush()
        except Exception:
            pass    # never let logging break the loop

    def on_messages_built(self, ctx: dict[str, Any],
                          items: list[dict]) -> list[dict]:
        # We snapshot the (post-retention) message list here so the saved
        # input.json reflects exactly what hit the API.
        ctx["_last_input_items"] = items
        return items

    def on_step_recorded(self, ctx: dict[str, Any], step) -> None:
        out_dir: Path = ctx["out_dir"]
        sn = step.idx
        # input.json — the multi-turn input as actually sent (post-retention)
        items = ctx.get("_last_input_items", [])
        (out_dir / f"step{sn:02d}-input.json").write_text(
            json.dumps({
                "model": ctx.get("model"),
                "instructions": ctx.get("system_prompt"),
                "input": _redact_input_for_log(items),
                "frame_hash": step.frame_hash,
            }, indent=2, ensure_ascii=False))
        # response + events
        (out_dir / f"step{sn:02d}-response.txt").write_text(step.raw_response or "")
        events = ctx.get("_last_events") or []
        with (out_dir / f"step{sn:02d}-events.jsonl").open("w") as f:
            for e in events:
                f.write(json.dumps(e, ensure_ascii=False) + "\n")

    def on_run_end(self, ctx: dict[str, Any]) -> None:
        out_dir: Path = ctx["out_dir"]
        history = ctx["history"]
        # transcript.json
        (out_dir / "transcript.json").write_text(json.dumps({
            "task": ctx["task"],
            "model": ctx.get("model"),
            "max_steps": ctx.get("max_steps"),
            "dry_run": ctx.get("dry_run"),
            "steps": [{
                "idx": s.idx,
                "thought": s.action.thought,
                "action": s.action.name,
                "x": s.action.x, "y": s.action.y,
                "text": s.action.text, "key": s.action.key,
                "amount": s.action.amount, "reason": s.action.reason,
                "verified_by_steps": s.action.verified_by_steps,
                "result": s.result,
                "elapsed_ms": s.elapsed_ms,
                "usage": s.usage,
                "screenshot": str(s.screenshot_path) if s.screenshot_path else None,
                "annotated":  str(s.annotated_path)  if s.annotated_path  else None,
            } for s in history],
        }, indent=2, ensure_ascii=False))

        # trace.md
        md = [f"# openseer trace: {ctx['task']}",
              f"\nmodel: `{ctx.get('model')}`  •  steps: {len(history)}/{ctx.get('max_steps')}  •  dry_run: {ctx.get('dry_run')}\n"]
        total_in = total_out = 0
        for s in history:
            a = s.action
            md.append(f"\n## step {s.idx}\n")
            if s.screenshot_path:
                md.append(f"- **screenshot**: ![]({Path(s.screenshot_path).name})")
            md.append(f"- **thought**: {a.thought}")
            bits = [f"`{a.name}`"]
            if a.x is not None:    bits.append(f"({a.x},{a.y})")
            if a.text:             bits.append(f"text={a.text!r}")
            if a.key:              bits.append(f"key={a.key}")
            if a.amount is not None: bits.append(f"amt={a.amount}")
            if a.reason:           bits.append(f"reason={a.reason!r}")
            if a.verified_by_steps:bits.append(f"verified_by={a.verified_by_steps}")
            md.append(f"- **action**: {' '.join(bits)}")
            md.append(f"- **result**: {s.result}")
            if s.usage:
                i_in  = s.usage.get("input_tokens", 0)
                i_out = s.usage.get("output_tokens", 0)
                total_in += i_in; total_out += i_out
                r_tok = (s.usage.get("output_tokens_details") or {}).get("reasoning_tokens", 0)
                md.append(f"- **tokens**: in={i_in}  out={i_out} (reasoning={r_tok})  •  {s.elapsed_ms}ms")
        md.append(f"\n---\n**totals**: input={total_in}  output={total_out}\n")
        (out_dir / "trace.md").write_text("\n".join(md))

        ctx["_totals"] = {"input_tokens": total_in, "output_tokens": total_out}

        # final.json — minimal footer for /show last and tooling.
        # If the run was aborted by an exception (model error, parse
        # error), TASK_FAILED was emitted; reflect that in the status
        # so /history doesn't display crashed runs as "cap" or "empty".
        last = history[-1] if history else None
        if self._failed_error is not None:
            final_status = "failed"
            error = self._failed_error
        else:
            error = None
            if last is None:
                final_status = "empty"
            elif last.action.name == "terminate":
                final_status = (last.action.status or "done").lower()
            elif last.action.name in ("done", "fail", "verify_failed"):
                final_status = last.action.name
            else:
                final_status = "cap"
        (out_dir / "final.json").write_text(json.dumps({
            "trace_id": ctx.get("trace_id"),
            "task": ctx["task"],
            "status": final_status,
            "error": error,
            "n_steps": len(history),
            "started_at": ctx.get("started_at"),
            "ended_at": time.time(),
            "totals": ctx["_totals"],
            "last_reason": (last.action.reason if last else None),
        }, indent=2, ensure_ascii=False))

        # We intentionally do NOT close `events.jsonl` here. The
        # RunReflection callback fires after us in the on_run_end
        # pass and emits `skill_proposed` / `skill_applied` /
        # `skill_discarded`, which need to land in the trace too so
        # the orb can resurrect a pending chip on reconnect. Closing
        # now drops those into a closed file handle and they vanish
        # from disk (codex P2 on the v0.1.6 tag push). The fh is
        # owned by this Callback instance and gets cleaned up when
        # `cbs` goes out of scope at the end of `agent.run()` — file-
        # descriptor lifetime is bounded by run duration, not by
        # daemon lifetime.

        if self.verbose:
            print(f"\n[agent] wrote {out_dir / 'transcript.json'}")
            print(f"[agent] wrote {out_dir / 'trace.md'}")
            print(f"[agent] tokens: input={total_in}  output={total_out}")
