"""Persists the full trajectory of a run to disk: per-step screenshots,
prompts, raw responses, SSE events, plus a final transcript.json and a
human-readable trace.md.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .base import Callback


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
      out_dir/task.txt
      out_dir/system_prompt.txt
      out_dir/step{N}-raw.png         (set by agent before this hook)
      out_dir/step{N}-action.png      (set by agent)
      out_dir/step{N}-input.json      (we write)
      out_dir/step{N}-response.txt    (we write)
      out_dir/step{N}-events.jsonl    (we write)
      out_dir/transcript.json
      out_dir/trace.md
    """

    def on_run_start(self, ctx: dict[str, Any]) -> None:
        out_dir: Path = ctx["out_dir"]
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "task.txt").write_text(ctx["task"])
        (out_dir / "system_prompt.txt").write_text(ctx["system_prompt"])

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

        print(f"\n[agent] wrote {out_dir / 'transcript.json'}")
        print(f"[agent] wrote {out_dir / 'trace.md'}")
        print(f"[agent] tokens: input={total_in}  output={total_out}")
